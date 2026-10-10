"""Scratch volume choices must not mutate the live process environment."""
import pytest

pytestmark = pytest.mark.api


def test_scratch_sqlite_pins_volume_without_changing_application_environment(tmp_path, monkeypatch):
    import os
    from lib.storage import StorageSupervisor

    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'required')
    external = tmp_path / 'must-not-attach-to-this.json'
    external.write_text('not a sidecar connection')
    monkeypatch.setenv('TOFU_STORAGE_CONNECTION_FILE', str(external))
    supervisor = StorageSupervisor(project_root=tmp_path, backend='sqlite',
                                   sqlite_fastpath_mode='off', connection_file='', startup_timeout=30)
    try:
        client = supervisor.start()
        assert os.environ['TOFU_STORAGE_FASTPATH'] == 'required'
        assert os.environ['TOFU_STORAGE_CONNECTION_FILE'] == str(external)
        fastpath = client.metrics(deadline=5)['fastpath']
        assert not fastpath['active']
        assert 'disabled' in fastpath['reason']
        assert client.health()['ready']
    finally:
        supervisor.stop()
