"""Executable budgets for read-only imports on high-latency data volumes."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_config_path_lookup_is_filesystem_pure_and_writes_create_parent(
    monkeypatch,
    tmp_path,
):
    import lib.config_dir as config_dir
    from lib.json_store import write_json_atomic

    config_root = tmp_path / 'data' / 'config'
    monkeypatch.setattr(config_dir, 'CONFIG_DIR', str(config_root))

    target = config_dir.config_path('example.json')
    assert target == str(config_root / 'example.json')
    assert not config_root.exists()

    write_json_atomic(target, {'ready': True}, fsync=False)
    assert config_root.is_dir()
    assert (config_root / 'example.json').is_file()


def test_feature_resolution_reuses_one_launch_snapshot(monkeypatch):
    import lib

    monkeypatch.delenv('DEBUG_MODE', raising=False)
    monkeypatch.delenv('OPTIMIZER_ENABLED', raising=False)
    monkeypatch.setattr(
        lib,
        '_SAVED_FEATURES',
        {'debug_mode': True, 'optimizer_enabled': False},
    )

    def unexpected_open(*_args, **_kwargs):
        raise AssertionError('feature resolution performed request-time I/O')

    monkeypatch.setattr(lib, 'open', unexpected_open, raising=False)
    assert lib._resolve_feature_flag('DEBUG_MODE', 'debug_mode', False) is True
    assert lib._resolve_feature_flag(
        'OPTIMIZER_ENABLED', 'optimizer_enabled', True) is False
    assert lib._resolve_feature_flag('MISSING_FLAG', 'missing', True) is True


def test_unrelated_lib_import_does_not_load_pricing_or_http_stack(tmp_path):
    environment = os.environ.copy()
    environment['TOFU_DATA_DIR'] = str(tmp_path / 'data')
    environment['TRADING_ENABLED'] = '0'
    code = (
        "import sys\n"
        "import lib.server_boot.lock\n"
        "assert 'lib.pricing' not in sys.modules\n"
        "assert 'lib.http_client' not in sys.modules\n"
        "import lib.pricing\n"
        "assert 'lib.http_client' not in sys.modules\n"
        "from lib.pricing import get_pricing_data\n"
        "get_pricing_data()\n"
        "assert 'lib.http_client' not in sys.modules\n"
    )

    completed = subprocess.run(
        [sys.executable, '-c', code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
