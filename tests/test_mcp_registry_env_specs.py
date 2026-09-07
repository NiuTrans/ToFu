"""Registry env_specs for the internal hope/llm MCP servers.

Pins the install-time credential/config surface the catalog advertises for the
Meituan-internal launchers. The actual server behaviour (tool profiles, login
timeouts, subprocess budgets) is owned by the sibling hope-mcp/llm-mcp repos;
ChatUI only needs to pass the env keys through ``env_specs`` so the install
modal can edit them and ``build_server_config`` forwards them into the launched
process env.
"""

import pytest

import lib.mcp.registry as reg

pytestmark = pytest.mark.unit


def _spec_by_key(server_id: str) -> dict[str, dict]:
    entry = reg.get_catalog_entry(server_id)
    assert entry is not None, server_id
    return {spec.get('key'): spec for spec in entry.get('env_specs', [])}


@pytest.mark.skipif(
    reg.is_opensource_build(),
    reason='internal MCP launchers are stripped from opensource builds',
)
def test_hope_env_specs_include_tool_profile_and_log_level():
    specs = _spec_by_key('hope')
    assert 'HOPE_MCP_TOOL_PROFILE' in specs
    assert 'HOPE_MCP_LOG_LEVEL' in specs

    profile = specs['HOPE_MCP_TOOL_PROFILE']
    assert profile.get('type') == 'select'
    assert {option['value'] for option in profile.get('options', [])} == {
        'core', 'extended', 'debug'}
    assert profile.get('required') is False
    assert specs['HOPE_MCP_LOG_LEVEL'].get('required') is False


@pytest.mark.skipif(
    reg.is_opensource_build(),
    reason='internal MCP launchers are stripped from opensource builds',
)
def test_llm_env_specs_include_login_parallel_and_log_level():
    specs = _spec_by_key('llm')
    for key in ('LLM_MCP_LOGIN_TIMEOUT', 'LLM_MCP_MAX_PARALLEL',
                'LLM_MCP_LOG_LEVEL', 'LLM_MCP_CA_BUNDLE'):
        assert key in specs, key
        assert specs[key].get('required') is False


@pytest.mark.skipif(
    reg.is_opensource_build(),
    reason='internal MCP launchers are stripped from opensource builds',
)
def test_new_env_keys_forward_into_built_config():
    env_values = {
        'HOPE_USERNAME': 'u',
        'HOPE_MCP_TOOL_PROFILE': 'debug',
        'HOPE_MCP_LOG_LEVEL': 'debug',
    }
    cfg = reg.build_server_config('hope', env_values)
    assert cfg is not None
    assert cfg.get('env', {}).get('HOPE_MCP_TOOL_PROFILE') == 'debug'
    assert cfg.get('env', {}).get('HOPE_MCP_LOG_LEVEL') == 'debug'

    llm_cfg = reg.build_server_config('llm', {
        'LLM_MIS': 'u',
        'LLM_MCP_CA_BUNDLE': '/tmp/internal-ca.pem',
    })
    assert llm_cfg is not None
    assert llm_cfg.get('env', {}).get('LLM_MCP_CA_BUNDLE') \
        == '/tmp/internal-ca.pem'


@pytest.mark.skipif(
    reg.is_opensource_build(),
    reason='internal MCP launchers are stripped from opensource builds',
)
def test_xuecheng_env_specs_include_toolset():
    specs = _spec_by_key('xuecheng')
    assert 'XUECHENG_TOOLSET' in specs
    assert specs['XUECHENG_TOOLSET'].get('required') is False


@pytest.mark.skipif(
    reg.is_opensource_build(),
    reason='internal MCP launchers are stripped from opensource builds',
)
def test_build_server_config_preserves_env_keys_not_in_specs():
    # Regression: catalog install/reconnect rebuilds the config row via
    # build_server_config; keys outside env_specs (hand-added in
    # mcp_servers.json, or re-exposed by the panel) were silently dropped,
    # e.g. XUECHENG_TOOLSET vanished and the server fell back to reader-only.
    cfg = reg.build_server_config('xuecheng', {
        'XUECHENG_MIS': 'u',
        'XUECHENG_TOOLSET': 'all',
        'HAND_ADDED_FUTURE_KEY': '1',
    })
    assert cfg is not None
    env = cfg.get('env', {})
    assert env.get('XUECHENG_TOOLSET') == 'all'
    assert env.get('HAND_ADDED_FUTURE_KEY') == '1'
