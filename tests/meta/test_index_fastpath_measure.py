"""The placement benchmark verifies equal work and restores all process inputs."""
import os

import pytest

from benchmarks.index_fastpath import run
from lib.project_mod import tree_index
from serverctl_pkg import local_placement as placement

pytestmark = pytest.mark.unit


@pytest.fixture
def volumes(tmp_path, monkeypatch):
    source, local = tmp_path / 'source', tmp_path / 'local'
    source.mkdir()
    local.mkdir()
    def select(*a, **k):
        enabled = os.environ.get('TOFU_INDEX_FASTPATH') == 'auto'
        return placement.Placement(enabled, placement.PURPOSE_INDEXES, 'auto', 'test',
                                   root=os.environ['TOFU_LOCAL_DIR'] + '/indexes')
    monkeypatch.setattr(placement, 'index_cache_placement', select)
    monkeypatch.setattr('lib.project_mod.tree_index_storage.index_cache_placement', select)
    return source, local


def test_equal_work_measurement_and_cleanup(volumes):
    env = dict(os.environ)
    sessions = tree_index.SESSIONS_DIR
    result = run(*volumes, entries=128, pairs=2)
    assert result['verified_roundtrips'] == 6
    assert result['entries'] == 128 and result['blob_bytes'] > 128
    assert len(result['source_sha256']) == 64
    for mode in ('off', 'auto'):
        assert len(result['samples'][mode]['publish_ms']) == 2
        assert len(result['samples'][mode]['load_ms']) == 2
    assert dict(os.environ) == env and tree_index.SESSIONS_DIR == sessions
    assert all(not list(path.iterdir()) for path in volumes)


def test_missing_work_refuses_performance_result(volumes, monkeypatch):
    env = dict(os.environ)
    monkeypatch.setattr(tree_index, '_load_disk', lambda root: None)
    with pytest.raises(AssertionError, match='roundtrip changed'):
        run(*volumes, entries=128, pairs=2)
    assert dict(os.environ) == env
    assert all(not list(path.iterdir()) for path in volumes)
