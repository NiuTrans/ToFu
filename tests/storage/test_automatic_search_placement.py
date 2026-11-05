"""Auto acceleration places only the reconstructible native search projection."""
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from lib.storage_authority_sql import projection_paths as paths
from serverctl_pkg import local_placement as placement

pytestmark = pytest.mark.unit


def test_unset_preference_uses_namespaced_local_index_without_writing(tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    monkeypatch.setattr(placement, 'index_cache_placement', lambda *a, **k: placement.Placement(
        True, placement.PURPOSE_INDEXES, 'auto', 'selected', root=str(cache)))
    first = paths.resolve_search_projection_paths(tmp_path / 'one', environ={})
    second = paths.resolve_search_projection_paths(tmp_path / 'two', environ={})
    assert first.directory.is_relative_to(cache / 'tofudb-search')
    assert first.directory != second.directory
    assert first.authority_directory == tmp_path / 'one/tofudb-authority'
    assert first.configured_directory == '' and first.default_directory == first.directory
    assert not cache.exists() and not first.authority_directory.exists()


def test_explicit_projection_path_bypasses_automatic_selection(tmp_path, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('explicit settings must not be replaced by placement')
    monkeypatch.setattr(placement, 'index_cache_placement', forbidden)
    result = paths.resolve_search_projection_paths(tmp_path / 'data', configured_directory=str(tmp_path / 'explicit'))
    assert result.directory.is_relative_to(tmp_path / 'explicit')


def test_unavailable_fast_volume_keeps_legacy_projection_and_selected_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(placement, 'index_cache_placement', lambda *a, **k: placement.Placement(
        False, placement.PURPOSE_INDEXES, 'auto', 'insufficient-space'))
    result = paths.resolve_search_projection_paths(tmp_path / 'data', environ={})
    assert result.directory == tmp_path / 'data/projections/tofudb-search'
    assert result.authority_directory == tmp_path / 'data/tofudb-authority'


def test_auto_namespace_symlink_is_not_followed(tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    cache.mkdir()
    monkeypatch.setattr(placement, 'index_cache_placement', lambda *a, **k: placement.Placement(
        True, placement.PURPOSE_INDEXES, 'auto', 'selected', root=str(cache)))
    first = paths.resolve_search_projection_paths(tmp_path / 'data', environ={})
    first.directory.parent.mkdir()
    first.directory.symlink_to(tmp_path / 'elsewhere', target_is_directory=True)
    result = paths.resolve_search_projection_paths(tmp_path / 'data', environ={})
    assert result.directory == tmp_path / 'data/projections/tofudb-search'


@pytest.mark.parametrize('preference', ['saved-reset', 'environment-reset', 'apply-reset'])
def test_empty_preference_uses_automatic_policy(tmp_path, monkeypatch, preference):
    import json
    cache = tmp_path / 'cache'
    monkeypatch.setattr(placement, 'index_cache_placement', lambda *a, **k: placement.Placement(
        True, placement.PURPOSE_INDEXES, 'auto', 'selected', root=str(cache)))
    root = tmp_path / 'data'
    env, kwargs = {}, {}
    if preference == 'saved-reset':
        (root / 'config').mkdir(parents=True)
        (root / 'config/server_config.json').write_text(json.dumps({paths.SEARCH_PROJECTION_CONFIG_KEY: ''}))
    elif preference == 'environment-reset':
        env[paths.SEARCH_PROJECTION_ENV] = ''
    else:
        kwargs['configured_directory'] = ''
    result = paths.resolve_search_projection_paths(root, environ=env, **kwargs)
    assert result.directory.is_relative_to(cache)
    assert not cache.exists()


@pytest.mark.parametrize('failure', [PermissionError('unwritable'), ValueError('full')])
def test_launch_setup_failure_falls_back_without_changing_authority(tmp_path, monkeypatch, failure):
    cache = tmp_path / 'cache'
    monkeypatch.setattr(placement, 'index_cache_placement', lambda *a, **k: placement.Placement(
        True, placement.PURPOSE_INDEXES, 'auto', 'selected', root=str(cache)))
    def full(path):
        raise failure
    monkeypatch.setattr(paths, '_check_projection_capacity', full)
    result = paths.prepare_search_projection_paths(tmp_path / 'data', environ={})
    assert result.directory == tmp_path / 'data/projections/tofudb-search'
    assert not result.directory.exists() and not result.authority_directory.exists()
    assert not (tmp_path / 'data/config').exists()


def test_launch_keeps_full_existing_index_available_for_native_gc(tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    monkeypatch.setattr(placement, 'index_cache_placement', lambda *a, **k: placement.Placement(
        True, placement.PURPOSE_INDEXES, 'auto', 'selected', root=str(cache)))
    chosen = paths.resolve_search_projection_paths(tmp_path / 'data', environ={})
    chosen.directory.mkdir(parents=True)
    cache.chmod(0o700)
    chosen.directory.parent.chmod(0o700)
    (chosen.directory / 'CONTROL').write_bytes(b'existing-index')
    def forbidden(path):
        raise AssertionError('existing full indexes must reach native GC')
    monkeypatch.setattr(paths, '_check_projection_capacity', forbidden)
    # Outside the copy's own capacity governance, whose critical volumes yield
    # (tests/storage/test_search_projection_capacity.py); not the host's /tmp.
    gib = 1024 ** 3
    monkeypatch.setattr(shutil, 'disk_usage', lambda path: SimpleNamespace(
        total=500 * gib, free=400 * gib, used=100 * gib))
    monkeypatch.setattr(os, 'statvfs', lambda path: SimpleNamespace(f_files=0, f_favail=0))
    assert paths.prepare_search_projection_paths(tmp_path / 'data', environ={}).directory == chosen.directory
    assert (chosen.directory / 'CONTROL').read_bytes() == b'existing-index'
