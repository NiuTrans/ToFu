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


def test_data_root_is_frozen_before_the_first_project_import():
    source = Path(__file__).with_name('conftest.py').read_text(encoding='utf-8')
    data_env = source.index("os.environ['TOFU_DATA_DIR']")
    first_project_import = min(
        source.index('from runtime_guards import'),
        source.index('import tofu_search.config'),
    )
    assert data_env < first_project_import


def test_webhook_store_is_inside_the_throwaway_test_root():
    from routes.api_v1 import webhooks

    test_root = Path(os.environ['TOFU_DATA_DIR']).resolve()
    store = Path(webhooks._STORE).resolve()
    assert store.is_relative_to(test_root)
    assert store != (Path(__file__).parents[1]
                     / 'data' / 'config' / 'webhooks.json').resolve()
