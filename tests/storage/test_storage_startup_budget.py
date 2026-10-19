"""Storage migration and ASGI lifespan share one bounded boot budget."""

import pytest

from lib.hypercorn_runtime import build_hypercorn_config
from lib.storage.startup_budget import (
    lifespan_startup_timeout,
    storage_startup_timeout,
)
from lib.storage.supervisor import StorageSupervisor, rpc_deadline_seconds


# serial: storage startup budget assertions are wall-clock sensitive —
# oversubscribed lanes starve the supervised storage boot past them.
pytestmark = [pytest.mark.unit, pytest.mark.serial]
def test_ordinary_storage_keeps_existing_short_startup_budgets(monkeypatch):
    environment = {
        'TOFU_STORAGE_FASTPATH': 'off',
        'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS': '1800',
    }

    assert storage_startup_timeout(environment) == 30.0
    assert lifespan_startup_timeout(environment) == 1860.0
    config = build_hypercorn_config(
        '127.0.0.1', 8000, keep_alive_timeout=5, environ=environment)
    assert config.startup_timeout == 1860.0

    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    assert StorageSupervisor()._startup_timeout == 30.0


def test_fastpath_seed_budget_reaches_sidecar_and_outer_lifespan(monkeypatch):
    environment = {
        'TOFU_STORAGE_FASTPATH': 'auto',
        'TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S': '240',
        'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS': '1800',
    }

    assert storage_startup_timeout(environment) == 240.0
    assert lifespan_startup_timeout(environment) == 1860.0
    config = build_hypercorn_config(
        '127.0.0.1', 8000, keep_alive_timeout=5, environ=environment)
    assert config.startup_timeout == 1860.0

    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'auto')
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S', '240')
    assert StorageSupervisor()._startup_timeout == 240.0
    assert StorageSupervisor(startup_timeout=2)._startup_timeout == 2.0


def test_fastpath_seed_budget_has_lean_fallback_and_hard_ceiling():
    malformed = {
        'TOFU_STORAGE_FASTPATH': 'required',
        'TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S': 'not-a-number',
    }
    excessive = {
        'TOFU_STORAGE_FASTPATH': 'auto',
        'TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S': '999999',
        'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS': '1800',
    }

    assert storage_startup_timeout(malformed) == 900.0
    assert storage_startup_timeout(excessive) == 3600.0
    assert lifespan_startup_timeout(excessive) == 3660.0


def test_backup_deadline_outlives_hypercorn_lifespan_cancellation():
    environment = {
        'TOFU_STORAGE_FASTPATH': 'auto',
        'TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S': '900',
        'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS': '21600',
    }

    assert lifespan_startup_timeout(environment) == 21660.0
    config = build_hypercorn_config(
        '127.0.0.1', 8000, keep_alive_timeout=5, environ=environment)
    assert config.startup_timeout == 21660.0


def test_rpc_deadline_defaults_to_five_seconds(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_RPC_DEADLINE_S', raising=False)
    assert rpc_deadline_seconds() == 5.0


def test_rpc_deadline_honours_network_volume_override(monkeypatch):
    """FUSE-backed authorities need a wider floor than the local-disk default."""
    monkeypatch.setenv('TOFU_STORAGE_RPC_DEADLINE_S', '20')
    assert rpc_deadline_seconds() == 20.0


def test_rpc_deadline_rejects_malformed_and_clamps(monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_RPC_DEADLINE_S', 'not-a-number')
    assert rpc_deadline_seconds() == 5.0
    monkeypatch.setenv('TOFU_STORAGE_RPC_DEADLINE_S', '0.001')
    assert rpc_deadline_seconds() == 1.0
    monkeypatch.setenv('TOFU_STORAGE_RPC_DEADLINE_S', '99999')
    assert rpc_deadline_seconds() == 120.0


def test_native_launch_durability_defaults_to_strict(monkeypatch):
    from lib.storage_authority_sql.native_launch import _durability_args

    monkeypatch.delenv('TOFU_DB_DURABILITY', raising=False)
    monkeypatch.delenv('TOFU_DB_WAL_FLUSH_INTERVAL_MS', raising=False)
    assert _durability_args() == []
    monkeypatch.setenv('TOFU_DB_DURABILITY', 'strict')
    assert _durability_args() == []


def test_native_launch_durability_normal_is_explicit_and_bounded(monkeypatch):
    from lib.storage_authority_sql.native_launch import _durability_args

    monkeypatch.setenv('TOFU_DB_DURABILITY', 'normal')
    monkeypatch.delenv('TOFU_DB_WAL_FLUSH_INTERVAL_MS', raising=False)
    assert _durability_args() == ['--durability', 'normal']
    monkeypatch.setenv('TOFU_DB_WAL_FLUSH_INTERVAL_MS', '250')
    assert _durability_args() == [
        '--durability', 'normal', '--wal-flush-interval-ms', '250']


def test_native_launch_durability_rejects_drift_fail_closed(monkeypatch):
    from lib.storage_authority_sql.native_launch import _durability_args

    # An interval without normal durability is a silent-drift trap, not a
    # hint: the bounded-RPO consent must be explicit and coherent.
    monkeypatch.delenv('TOFU_DB_DURABILITY', raising=False)
    monkeypatch.setenv('TOFU_DB_WAL_FLUSH_INTERVAL_MS', '250')
    with pytest.raises(RuntimeError):
        _durability_args()
    monkeypatch.setenv('TOFU_DB_DURABILITY', 'eventually-consistent')
    monkeypatch.delenv('TOFU_DB_WAL_FLUSH_INTERVAL_MS', raising=False)
    with pytest.raises(RuntimeError):
        _durability_args()
    monkeypatch.setenv('TOFU_DB_DURABILITY', 'normal')
    monkeypatch.setenv('TOFU_DB_WAL_FLUSH_INTERVAL_MS', '0')
    with pytest.raises(RuntimeError):
        _durability_args()
    monkeypatch.setenv('TOFU_DB_WAL_FLUSH_INTERVAL_MS', '1001')
    with pytest.raises(RuntimeError):
        _durability_args()


def test_native_fastpath_seed_uses_the_same_bounded_outer_startup_budget():
    environment = {'TOFU_DB_FASTPATH_DIR': '/explicit/local/base',
                   'TOFU_STORAGE_FASTPATH': 'off',
                   'TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S': '2400',
                   'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS': '120'}
    assert storage_startup_timeout(environment) == 2400.0
    assert lifespan_startup_timeout(environment) == 2460.0
