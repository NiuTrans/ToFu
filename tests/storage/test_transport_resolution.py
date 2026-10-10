"""Automatic native-transport resolution (TOFUDB_TRANSPORT=auto)."""

from __future__ import annotations

import pytest

from lib.storage.supervisor import StorageSupervisor
from lib.storage import volume_class

pytestmark = pytest.mark.unit


@pytest.fixture()
def supervisor(tmp_path):
    return StorageSupervisor(project_root=tmp_path)


def test_transport_defaults_to_auto(supervisor, monkeypatch):
    monkeypatch.delenv('TOFUDB_TRANSPORT', raising=False)
    assert supervisor._configured_transport() == 'auto'


def test_transport_rejects_unknown_values(supervisor, monkeypatch):
    monkeypatch.setenv('TOFUDB_TRANSPORT', 'socket')
    with pytest.raises(
            RuntimeError,
            match='TOFUDB_TRANSPORT must be auto, daemon or embedded'):
        supervisor._configured_transport()


def test_explicit_transports_pass_through(supervisor, monkeypatch):
    for explicit in ('daemon', 'embedded'):
        monkeypatch.setenv('TOFUDB_TRANSPORT', explicit)
        assert supervisor._resolve_transport() == explicit


def test_auto_resolves_embedded_on_a_local_volume(supervisor, monkeypatch):
    monkeypatch.delenv('TOFUDB_TRANSPORT', raising=False)
    monkeypatch.setattr(volume_class, 'volume_class', lambda _path: 'local')
    assert supervisor._resolve_transport() == 'embedded'


def test_auto_resolves_daemon_on_a_network_volume(supervisor, monkeypatch):
    monkeypatch.delenv('TOFUDB_TRANSPORT', raising=False)
    monkeypatch.setattr(volume_class, 'volume_class', lambda _path: 'network')
    assert supervisor._resolve_transport() == 'daemon'


def test_auto_resolves_embedded_when_the_volume_is_unobservable(
        supervisor, monkeypatch):
    # Unknown mirrors the engine's always-safe Unknown→Strict tiering:
    # strict-only embedded stays correct everywhere; the risk is latency
    # on an unobserved network mount, never durability.
    monkeypatch.delenv('TOFUDB_TRANSPORT', raising=False)
    monkeypatch.setattr(volume_class, 'volume_class', lambda _path: 'unknown')
    assert supervisor._resolve_transport() == 'embedded'


def test_native_fastpath_resolves_daemon_and_rejects_explicit_embedded(supervisor, monkeypatch, tmp_path):
    monkeypatch.setenv('TOFU_DB_FASTPATH_DIR', str(tmp_path))
    monkeypatch.setenv('TOFUDB_TRANSPORT', 'auto')
    assert supervisor._resolve_transport() == 'daemon'
    monkeypatch.setenv('TOFUDB_TRANSPORT', 'embedded')
    with pytest.raises(RuntimeError, match='fastpath requires daemon'):
        supervisor._resolve_transport()


def test_native_sync_interval_requires_an_explicit_front(supervisor, monkeypatch):
    monkeypatch.delenv('TOFU_DB_FASTPATH_DIR', raising=False)
    monkeypatch.setenv('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS', '1000')
    with pytest.raises(RuntimeError, match='requires TOFU_DB_FASTPATH_DIR'):
        supervisor._resolve_transport()
