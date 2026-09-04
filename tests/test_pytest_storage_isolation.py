"""Pytest must freeze all writable paths under throwaway roots."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_conftest_has_one_module_identity_and_one_data_root():
    import tests.conftest as packaged

    assert sys.modules.get('conftest') is packaged
    assert packaged.os.environ['TOFU_DATA_DIR'] == os.environ['TOFU_DATA_DIR']
    assert Path(packaged._PYTEST_DATA_ROOT).parent == packaged._PYTEST_ROOT_PARENT
    assert Path(packaged._PYTEST_STORAGE_ROOT).parent == packaged._PYTEST_ROOT_PARENT


def test_data_root_is_frozen_before_the_first_project_import():
    source = Path(__file__).with_name('conftest.py').read_text(encoding='utf-8')
    lifecycle_scrub = source.index(
        '_CLEARED_INHERITED_LIFECYCLE_ENV_NAMES =')
    data_env = source.index("os.environ['TOFU_DATA_DIR']")
    first_project_import = min(
        source.index('from runtime_guards import'),
        source.index('import tofu_search.config'),
    )
    assert lifecycle_scrub < data_env < first_project_import


def test_inherited_lifecycle_environment_is_removed_as_one_contract():
    import tests.conftest as packaged

    environment = {
        name: 'ambient-production-value'
        for name in packaged._INHERITED_LIFECYCLE_ENV_NAMES
    }
    environment['UNRELATED_TEST_INPUT'] = 'keep-me'

    removed = packaged._clear_inherited_lifecycle_environment(environment)

    assert set(removed) == set(packaged._INHERITED_LIFECYCLE_ENV_NAMES)
    assert not set(packaged._INHERITED_LIFECYCLE_ENV_NAMES) & set(environment)
    assert environment == {'UNRELATED_TEST_INPUT': 'keep-me'}


def test_webhook_store_is_inside_the_throwaway_test_root():
    from routes.api_v1 import webhooks

    test_root = Path(os.environ['TOFU_DATA_DIR']).resolve()
    store = Path(webhooks._STORE).resolve()
    assert store.is_relative_to(test_root)
    assert store != (Path(__file__).parents[1]
                     / 'data' / 'config' / 'webhooks.json').resolve()


def test_crashed_test_root_reclaim_is_owner_safe_and_bounded(
        tmp_path, monkeypatch):
    import tests.conftest as packaged

    live = tmp_path / 'tofu-test-data-gw0-pid-42-live'
    dead_a = tmp_path / 'tofu-test-data-gw1-pid-43-dead_a'
    dead_b = tmp_path / 'tofu-test-storage-pid-44-dead_b'
    legacy = tmp_path / 'tofu-test-storage-legacy'
    outside = tmp_path / 'outside'
    symlink = tmp_path / 'tofu-test-storage-pid-45-symlink'
    for directory in (live, dead_a, dead_b, legacy, outside):
        directory.mkdir()
    symlink.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        packaged, '_pytest_root_owner_is_alive', lambda pid: pid == 42)

    first = packaged._reclaim_stale_pytest_roots(
        tmp_path, current_pid=42, reclaim_limit=1)

    assert len(first['removed']) == 1
    assert live.is_dir()
    assert legacy.is_dir()
    assert symlink.is_symlink()
    assert sum(path.is_dir() for path in (dead_a, dead_b)) == 1

    second = packaged._reclaim_stale_pytest_roots(
        tmp_path, current_pid=42, reclaim_limit=8)
    assert len(second['removed']) == 1
    assert not dead_a.exists() and not dead_b.exists()
