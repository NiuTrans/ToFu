"""Malformed selected shared paths stay terminal and never expose raw exceptions."""
import pytest
import tofu_mtsso_auth as auth

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('value', ['relative.json', '   '])
def test_auto_selected_invalid_token_path_uses_public_envelope(monkeypatch, tmp_path, value):
    for key in tuple(auth.os.environ):
        if key.startswith(('MTSSO_', 'AGENT_SSO_')):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MTSSO_AUTH_MODE', 'auto')
    monkeypatch.setenv('MTSSO_SESSION_TOKEN_FILE', value)
    monkeypatch.setattr(auth, '_select_official', lambda: False)
    monkeypatch.setattr(auth, '_run', lambda *a, **k: pytest.fail('selection never spawns'))
    auth.clear()
    try:
        result = auth.provider_status()
        assert result['ok'] is False
        assert result['error_code'] == 'SSO_SESSION_CONFIGURATION'
        assert result['retryable'] is False
        assert 'login_required' not in result
        assert result['auth_state'] == 'blocked'
        assert value not in result['error']
        with pytest.raises(auth.OfficialAuthError):
            auth.enabled()
    finally:
        auth.clear()
