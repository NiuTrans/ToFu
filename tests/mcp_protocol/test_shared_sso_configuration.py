"""One internal SSO configuration contract, principal-bound MCP environment."""
import pytest
from lib.mcp import registry
from lib.mcp.stdio_environment import build_stdio_environment

pytestmark = pytest.mark.unit


@pytest.mark.skipif(registry.is_opensource_build(), reason='internal providers are not exported')
def test_internal_mcps_share_same_sso_mode_and_paths():
    for server in ('hope', 'llm', 'xuecheng'):
        entry = registry.get_catalog_entry(server)
        specs = {item['key']: item for item in entry['env_specs']}
        assert {item['value'] for item in specs['MTSSO_AUTH_MODE']['options']} == {'auto','official','shared','legacy'}
        config = registry.build_server_config(server, {
            'MTSSO_AUTH_MODE':'shared','MTSSO_MIS':'alice',
            'MTSSO_SESSION_SDK':'/installed/auth.js','MTSSO_SESSION_TOKEN_FILE':'/private/session.json'})
        for key in ('MTSSO_AUTH_MODE','MTSSO_MIS','MTSSO_SESSION_SDK','MTSSO_SESSION_TOKEN_FILE'):
            assert key in config['env']
            assert specs[key]['secret'] is False
            assert specs[key]['required'] is False


def test_stdio_cache_identity_cannot_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('TOFU_MCP_OWNER_ID', 'inherited-identity')
    monkeypatch.setattr('lib.mcp.client._vendor._ensure_writable_caches', lambda *a, **kw: None)
    monkeypatch.setattr('lib.mcp.client._vendor._propagate_proxy_env', lambda *a, **kw: None)
    config = {'env':{'TOFU_MCP_OWNER_ID':'wrong-owner','MTSSO_MIS':'alice'}}
    output = build_stdio_environment(config, owner_user_id=7, prepare_directories=False)
    assert output['TOFU_MCP_OWNER_ID'] == 'tofu:7'
    assert config['env']['TOFU_MCP_OWNER_ID'] == 'wrong-owner'
    assert 'TOFU_MCP_OWNER_ID' not in build_stdio_environment({}, prepare_directories=False)
    with pytest.raises(ValueError): build_stdio_environment(config, prepare_directories=False)
    for owner in (True, 0, -1, '7'):
        with pytest.raises(ValueError): build_stdio_environment({}, owner_user_id=owner, prepare_directories=False)
