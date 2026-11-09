"""The storage-medium probe measures every declared path and leaves nothing behind."""
import os

import pytest

from benchmarks.storage_medium_probe import run

pytestmark = pytest.mark.unit


def test_probe_reports_every_operation_and_removes_its_scratch(tmp_path):
    before = sorted(os.listdir(tmp_path))
    report = run(tmp_path, seq_mib=2, appends_count=4, publish_count=3,
                 metadata_count=5, random_count=8, concurrent_count=2)

    assert report['scratch_removed'] is True
    assert sorted(os.listdir(tmp_path)) == before
    assert report['volume']['storage_class']
    assert report['wal_append_4k_fdatasync']['n'] == 4
    assert report['wal_overwrite_4k_fdatasync']['n'] == 4
    assert report['wal_append_64k_fdatasync']['n'] == 2
    assert report['wal_append_4k_write_only']['n'] == 20
    assert report['publish_4k_block']['n'] == 3
    metadata = report['metadata']
    for name in ('create_4k', 'stat_hit_first', 'stat_hit_repeat', 'stat_miss',
                 'open_read_close_warm', 'open_read_close_after_drop', 'rename', 'unlink'):
        assert metadata[name]['n'] == 5, name
    assert metadata['readdir']['n'] == 10
    data = report['data']
    assert data['seq_write_fsync_mb_s'] > 0 and data['seq_read_after_drop_mb_s'] > 0
    assert data['random_read_4k_after_drop']['n'] == 8
    assert data['random_write_4k_fdatasync']['n'] == 2
    assert 'random_read_4k_o_direct' in data or data['o_direct']
    assert report['concurrent8_append_4k_fdatasync']['n'] == 16
    assert report['concurrent8_publish_4k_block']['n'] == 8
    assert 'errors' not in report['concurrent8_append_4k_fdatasync']


def test_probe_refuses_a_missing_scratch_root(tmp_path):
    with pytest.raises(SystemExit):
        run(tmp_path / 'absent', seq_mib=1)
    assert not (tmp_path / 'absent').exists()
