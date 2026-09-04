"""tests/test_mcp_tool_toggle.py — per-tool enable/disable on MCP servers.

Epic pt_53065dbe86bb4286: until now an MCP server was all-or-nothing — every
discovered tool's schema rode every request (~190 in one deployment). The
user can now disable individual tools per server (Settings → MCP), persisted
as ``disabled_tools`` in the server config row. A disabled tool is:

  * HIDDEN from the model's tool list (get_openai_tool_defs filters it),
  * ABSENT from the safety map (get_tool_safety — no dangling partition row),
  * REFUSED at call time (call_tool raises — stale history / in-flight
    protection),
  * PRESERVED across config migrations (unknown keys pass through).

The API surface (PUT /api/v1/mcp/servers/<name>/tools) persists the row and
hot-applies to the live bridge via set_disabled_tools (no reconnect).
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.unit


# ── fixtures ──────────────────────────────────────────────────────────

def _bridge_with(tools, configs=None):
    """A minimal live bridge: two servers' tools + optional config rows."""
    from lib.mcp.client._bridge import MCPBridge
    from lib.mcp.types import make_namespaced_name

    b = MCPBridge.__new__(MCPBridge)
    b._lock = threading.RLock()
    b._tool_index = {}
    b._servers = {}
    b._configs = configs or {}
    b._started = True
    for server, names in tools.items():
        b._servers[server] = type('H', (), {
            'session': object(), 'tools': [], 'config': {}})()
        for name in names:
            ns = make_namespaced_name(server, name)
            b._tool_index[ns] = {
                'server_name': server,
                'tool_name': name,
                'namespaced_name': ns,
                'description': 'd',
                'input_schema': {'type': 'object', 'properties': {}},
                'openai_def': {'type': 'function',
                               'function': {'name': ns, 'description': 'd',
                                            'parameters': {}}},
                'read_only_hint': False,
            }
    return b


# ── 1. discovery filter ───────────────────────────────────────────────

def test_disabled_tool_hidden_from_model_surface():
    b = _bridge_with(
        {'srv': ['keep_tool', 'drop_tool']},
        {'srv': {'disabled_tools': ['drop_tool']}},
    )
    names = [d['function']['name'] for d in b.get_openai_tool_defs()]
    assert names == ['mcp__srv__keep_tool']


def test_safety_map_drops_disabled_tool():
    """No dangling write-partition row for a tool the model cannot see."""
    b = _bridge_with(
        {'srv': ['keep_tool', 'drop_tool']},
        {'srv': {'disabled_tools': ['drop_tool']}},
    )
    safety = b.get_tool_safety()
    assert set(safety) == {'mcp__srv__keep_tool'}


def test_filter_is_per_server():
    """Disabling a tool on one server must not hide a same-named tool on
    another server (names collide across servers by design)."""
    b = _bridge_with(
        {'a': ['search'], 'b': ['search']},
        {'a': {'disabled_tools': ['search']}},
    )
    names = [d['function']['name'] for d in b.get_openai_tool_defs()]
    assert names == ['mcp__b__search']


def test_no_config_row_means_everything_enabled():
    """A server without disabled_tools (the pre-feature default) ships all
    its tools — and minimal fake bridges without _configs still work."""
    b = _bridge_with({'srv': ['one', 'two']})
    b._configs = {}  # explicit empty
    assert len(b.get_openai_tool_defs()) == 2
    # And a bridge whose _configs attribute was never set at all (__new__
    # fixtures from other suites) must not raise either.
    del b._configs
    assert len(b.get_openai_tool_defs()) == 2


# ── 2. call-time refusal ──────────────────────────────────────────────

def test_call_tool_refuses_disabled_tool():
    b = _bridge_with(
        {'srv': ['drop_tool']},
        {'srv': {'disabled_tools': ['drop_tool']}},
    )
    with pytest.raises(ValueError, match='disabled by user'):
        b.call_tool('mcp__srv__drop_tool', {})


def test_enabled_tool_passes_the_disabled_gate():
    """The refusal must fire ONLY for disabled tools: an enabled tool on a
    server that HAS a disabled list must sail past the gate (it then fails
    later on the fake session, which is fine — we assert the error is NOT
    the disabled one)."""
    b = _bridge_with(
        {'srv': ['keep_tool', 'drop_tool']},
        {'srv': {'disabled_tools': ['drop_tool']}},
    )
    try:
        b.call_tool('mcp__srv__keep_tool', {})
    except Exception as e:
        # The disabled-gate refusal must NOT fire for an enabled tool — in
        # ANY exception class. The old ValueError-only check + bare-pass let a
        # misfire of any other class sail through unobserved. Any other error
        # (the fake session cannot serve a real call) is fine.
        assert 'disabled by user' not in str(e), (
            f'the disabled gate fired on an ENABLED tool: {e}')


# ── 3. hot update ─────────────────────────────────────────────────────

def test_set_disabled_tools_hot_applies_without_reconnect():
    b = _bridge_with({'srv': ['a', 'b']})
    assert len(b.get_openai_tool_defs()) == 2
    before_fingerprint, before_projection = b.get_tool_catalog_projection()
    before_search_text = b.get_tool_catalog_search_text_projection()
    b.set_disabled_tools('srv', ['b'])
    assert [d['function']['name'] for d in b.get_openai_tool_defs()] == [
        'mcp__srv__a']
    after_fingerprint, after_projection = b.get_tool_catalog_projection()
    assert after_projection is not before_projection
    assert after_fingerprint != before_fingerprint
    assert [row['tool_name'] for row in after_projection] == ['a']
    after_search_text = b.get_tool_catalog_search_text_projection()
    assert after_search_text is not before_search_text
    assert set(after_search_text) == {'mcp__srv__a'}
    b.set_disabled_tools('srv', ['b', 'b'])
    assert b.get_tool_catalog_projection()[1] is after_projection
    assert b.get_tool_catalog_search_text_projection() is after_search_text
    # And the live handle's config copy is kept in sync (consistency for any
    # consumer reading handle.config).
    assert b._servers['srv'].config['disabled_tools'] == ['b']
    # Re-enabling works the same way.
    b.set_disabled_tools('srv', [])
    assert len(b.get_openai_tool_defs()) == 2


def test_set_disabled_tools_normalises_input():
    """Duplicates and non-strings are dropped; the stored list is sorted for
    byte-stable config diffs."""
    b = _bridge_with({'srv': ['a']})
    b.set_disabled_tools('srv', ['z', 'z', 'a', 42, None])
    assert b._configs['srv']['disabled_tools'] == ['a', 'z']


# ── 4. config persistence ─────────────────────────────────────────────

def test_config_round_trip_preserves_disabled_tools(tmp_path, monkeypatch):
    """The row survives save → load untouched (JSON-tolerant format, no
    migration needed)."""
    from lib.mcp import config as mcp_config

    cfg_file = tmp_path / 'mcp_servers.json'
    monkeypatch.setattr(mcp_config, '_config_path', lambda: str(cfg_file))
    mcp_config.save_mcp_config({
        'srv': {'command': 'x', 'enabled': True,
                'disabled_tools': ['drop_tool']},
    })
    loaded = mcp_config.load_mcp_config()
    assert loaded['srv']['disabled_tools'] == ['drop_tool']


def test_stale_migration_preserves_disabled_tools(tmp_path, monkeypatch):
    """_migrate_stale_entries rewrites command/args IN PLACE — a row carrying
    disabled_tools must keep it through a migration."""
    from lib.mcp import config as mcp_config

    cfg_file = tmp_path / 'mcp_servers.json'
    monkeypatch.setattr(mcp_config, '_config_path', lambda: str(cfg_file))
    mcp_config.save_mcp_config({
        'overleaf': {
            'command': 'overleaf-mcp',
            'args': [],
            'disabled_tools': ['compile_project'],
        },
    })
    loaded = mcp_config.load_mcp_config()
    assert loaded['overleaf']['command'] == 'uvx'  # migration fired
    assert loaded['overleaf']['args'] == [
        '--exclude-newer', '2026-08-14T00:00:00Z',
        '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp',
    ]
    assert loaded['overleaf']['disabled_tools'] == ['compile_project']


def test_stale_overleaf_mcp1_override_migrates_to_v2(tmp_path, monkeypatch):
    """The production row once forced mcp<2 on every fresh uvx resolve.
    Loading config must remove that override without touching credentials."""
    from lib.mcp import config as mcp_config

    cfg_file = tmp_path / 'mcp_servers.json'
    monkeypatch.setattr(mcp_config, '_config_path', lambda: str(cfg_file))
    mcp_config.save_mcp_config({
        'overleaf': {
            'command': 'uvx',
            'args': [
                '--from', 'overleaf-mcp-plus[compile]>=0.1.3',
                '--with', 'mcp<2', 'overleaf-mcp',
            ],
            'env': {'OVERLEAF_SESSION': 'secret'},
        },
    })

    loaded = mcp_config.load_mcp_config()

    assert loaded['overleaf']['args'] == [
        '--exclude-newer', '2026-08-14T00:00:00Z',
        '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp',
    ]
    assert loaded['overleaf']['env'] == {'OVERLEAF_SESSION': 'secret'}


def test_intermediate_overleaf_v2_pin_gains_reviewed_cutoff(tmp_path, monkeypatch):
    """A v2 pin must remain cold-reconnectable under an old live parent."""
    from lib.mcp import config as mcp_config

    cfg_file = tmp_path / 'mcp_servers.json'
    monkeypatch.setattr(mcp_config, '_config_path', lambda: str(cfg_file))
    mcp_config.save_mcp_config({
        'overleaf': {
            'command': 'uvx',
            'args': [
                '--from', 'overleaf-mcp-plus[compile]==0.3.0', 'overleaf-mcp',
            ],
            'env': {},
        },
    })

    loaded = mcp_config.load_mcp_config()

    assert loaded['overleaf']['args'] == [
        '--exclude-newer', '2026-08-14T00:00:00Z',
        '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp',
    ]


def test_published_overleaf_030_pin_migrates_to_031(tmp_path, monkeypatch):
    """Existing exact 0.3.0 installs must receive the integration fixes."""
    from lib.mcp import config as mcp_config

    cfg_file = tmp_path / 'mcp_servers.json'
    monkeypatch.setattr(mcp_config, '_config_path', lambda: str(cfg_file))
    mcp_config.save_mcp_config({
        'overleaf': {
            'command': 'uvx',
            'args': [
                '--exclude-newer', '2026-08-02T00:00:00Z',
                '--from', 'overleaf-mcp-plus[compile]==0.3.0', 'overleaf-mcp',
            ],
            'env': {'OVERLEAF_SESSION': 'secret'},
            'disabled_tools': ['delete_file'],
        },
    })

    loaded = mcp_config.load_mcp_config()

    assert loaded['overleaf']['args'] == [
        '--exclude-newer', '2026-08-14T00:00:00Z',
        '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp',
    ]
    assert loaded['overleaf']['env'] == {'OVERLEAF_SESSION': 'secret'}
    assert loaded['overleaf']['disabled_tools'] == ['delete_file']


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
