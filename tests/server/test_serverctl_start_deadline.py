"""A source preflight must not outlive its lifecycle command's HTTP owner."""
import pytest

import serverctl

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('action', ['start', 'stop', 'restart'])
def test_lifecycle_request_can_wait_for_source_preparation(monkeypatch, action):
    monkeypatch.setattr(serverctl, 'ensure_manager', lambda: None)

    def prepared(path, body, timeout):
        if timeout <= 5.0:
            raise TimeoutError('source preparation outlived the ordinary control request')
        assert path == '/' + action
        assert body['projectPath'] == serverctl.PROJECT
        return {'ok': True, 'prepared': True}

    monkeypatch.setattr(serverctl, '_request', prepared)
    assert serverctl._post(action) == {'ok': True, 'prepared': True}
