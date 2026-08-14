"""tests/test_tool_inventory.py — Live tool-registry inventory (Settings → 工具).

Pins the read side of the unified tool registry
(``lib/tools/registry/_introspect.build_tool_inventory`` + the
``GET /api/v1/tools`` surface), born 2026-08-06 from the owner directive
"show me every tool registered right now, organized by group".

What is pinned:

  * every built-in spec family appears in the inventory, grouped by its
    declared category, with its full ``provides`` surface listed (gated-off
    families list their tools as disabled rows, never vanish);
  * the two-phase evaluation semantics — capability-phase families
    (memory/scheduler/todo/skills) attach on ``has_base_tools`` and must
    report ``on`` under the reference context (a naive per-spec evaluation
    reported them all closed — the first draft of the builder did exactly
    that);
  * every built-in spec carries a human-readable ``gate`` string (the
    "where do I turn this on" promise the panel renders);
  * plugin specs are reported as registered-but-hidden
    (``plugin_not_allowlisted``) rather than silently absent;
  * ``capabilities._tools_summary`` is derived from the same inventory
    (the 2026-08-06 fix for its hand-maintained 5-group drift);
  * the /api/v1/tools route is registered on the blueprint.

Run isolated (project convention): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _inventory():
    from lib.tools.registry._introspect import build_tool_inventory
    return build_tool_inventory()


def _families(inv):
    out = {}
    for g in inv['groups']:
        for f in g['families']:
            out[(g['id'], f['key'])] = f
    return out


# ── Coverage: every built-in spec shows up, in its declared group ──────

def test_every_builtin_spec_appears_in_its_category_group():
    from lib.tools import all_specs
    fams = _families(_inventory())
    missing = []
    for spec in all_specs():
        if spec.source != 'builtin':
            continue
        gid = spec.category or 'other'
        # Data-backed families may deliberately stay out of the global panel
        # until they have a live callable surface. Their feature tests pin
        # both the absent and active catalogue states.
        if (gid, spec.key) not in fams and not spec.catalog_active_only:
            missing.append(f'{gid}/{spec.key}')
    assert not missing, (
        f'built-in specs missing from the inventory: {missing} — the panel '
        'would silently hide a registered family'
    )


def test_full_provides_surface_listed_even_when_gated_off():
    """The browser family (extension disconnected in tests) must list all 13
    declared tools as disabled rows — the panel shows the REGISTERED
    surface, not only the model-visible subset."""
    fams = _families(_inventory())
    browser = fams[('browser', 'browser')]
    assert browser['gate_state'] == 'off'
    names = {t['name'] for t in browser['tools']}
    assert {'browser_navigate', 'browser_click', 'browser_execute_js',
            'browser_read_page', 'browser_list_tabs'} <= names
    assert all(not t['enabled'] for t in browser['tools'])
    assert browser['counts']['total'] == len(browser['tools'])


# ── Two-phase evaluation semantics (the naive-evaluation regression) ────

@pytest.mark.parametrize('key', ['memory', 'scheduler', 'todo'])
def test_capability_families_attach_on_has_base_tools(key):
    """read_files is always on ⇒ has_base_tools=True ⇒ the always-on
    capability families must report ON under the reference context."""
    fams = _families(_inventory())
    fam = next(f for (_g, k), f in fams.items() if k == key)
    assert fam['gate_state'] == 'on', (
        f'{key} reported {fam["gate_state"]} ({fam["gate_reason"]}) — the '
        'inventory must thread ctx.has_base_tools between phases exactly '
        'like assemble_tool_list, or every always-on family reads as off'
    )
    assert any(t['enabled'] for t in fam['tools'])


def test_skills_family_reflects_installed_eligible_packages(monkeypatch):
    """Do not advertise ``load_skill`` when there is nothing it can load."""
    import lib.skills

    monkeypatch.setattr(lib.skills, 'list_skills', lambda *a, **k: [])
    skills = next(
        family for (_group, key), family in _families(_inventory()).items()
        if key == 'skills')
    assert skills['gate_state'] == 'off'
    assert skills['gate_reason'] == 'gate_closed'
    assert not any(tool['enabled'] for tool in skills['tools'])

    monkeypatch.setattr(lib.skills, 'list_skills', lambda *a, **k: [{
        'id': 'review', 'enabled': True, 'eligible': True,
    }])
    skills = next(
        family for (_group, key), family in _families(_inventory()).items()
        if key == 'skills')
    assert skills['gate_state'] == 'on'
    assert any(tool['name'] == 'load_skill' and tool['enabled']
               for tool in skills['tools'])


def test_reference_context_is_plain_chat_defaults():
    inv = _inventory()
    assert inv['scope'] == 'global_registry'
    ref = inv['reference']
    assert ref['search_mode'] == 'multi'
    assert ref['project_attached'] is False
    # And the always-on quartet is the active core of a plain chat turn.
    assert inv['totals']['active'] >= 4  # web_search/fetch/read_files/inspect


def test_totals_are_internally_consistent():
    inv = _inventory()
    per_family = sum(f['counts']['total'] for g in inv['groups']
                     for f in g['families'])
    assert inv['totals']['tools'] == per_family
    active = sum(f['counts']['active'] for g in inv['groups']
                 for f in g['families'])
    assert inv['totals']['active'] == active


# ── The human-readable gate contract ────────────────────────────────────

def test_every_builtin_spec_declares_a_gate_hint():
    """A built-in without a `gate` string renders as an unexplained dead
    family in the panel. New built-ins must fill it (presentation-only)."""
    from lib.tools import all_specs
    missing = [s.key for s in all_specs()
               if s.source == 'builtin' and not s.gate.strip()]
    assert not missing, (
        f'built-in specs without a gate hint: {missing} — the Settings → '
        '工具 panel prints this string as "开启方式" for gated-off families'
    )


# ── Plugin visibility: registered-but-hidden is reported, not vanished ──

def test_plugin_spec_reported_as_not_allowlisted():
    from lib.tools import ToolSpec, register_tool_spec
    from lib.tools import registry as _reg
    spec = ToolSpec(
        key='_inv_fake_plugin',
        build=lambda ctx: [{'type': 'function',
                            'function': {
                                'name': '_inv_fake_tool',
                                'description': 'Search the fake plugin',
                            }}],
        phase='base', category='testplug',
        provides=frozenset({'_inv_fake_tool'}),
        source='plugin', plugin_name='_inv_fake',
    )
    try:
        register_tool_spec(spec)
        fams = _families(_inventory())
        fam = fams.get(('testplug', '_inv_fake_plugin'))
        assert fam is not None, 'plugin spec must appear in the inventory'
        assert fam['gate_state'] == 'off'
        assert fam['gate_reason'] == 'plugin_not_allowlisted'
        assert fam['source'] == 'plugin' and fam['plugin_name'] == '_inv_fake'
        row = next(t for t in fam['tools'] if t['name'] == '_inv_fake_tool')
        assert row['enabled'] is False
        assert row['description'] == 'Search the fake plugin'
    finally:
        _reg._TOOL_SPECS[:] = [s for s in _reg._TOOL_SPECS
                               if s.key != '_inv_fake_plugin']
        _reg._REGISTERED_KEYS.discard('_inv_fake_plugin')


# ── Schema fidelity: contributed tools carry desc/required/badges ───────

def test_contributed_tools_have_schema_metadata():
    fams = _families(_inventory())
    search = fams[('search', 'search')]
    tool = next(t for t in search['tools'] if t['name'] == 'web_search')
    assert tool['enabled'] is True
    assert tool['description'], 'web_search must carry its schema description'
    assert tool['handler'] is True, 'web_search must show a bound handler'


def test_write_badge_follows_the_partition():
    fams = _families(_inventory())
    memory = fams[('memory', 'memory')]
    by_name = {t['name']: t for t in memory['tools']}
    assert by_name['create_memory']['write'] is True
    assert by_name['search_memories']['write'] is False


def test_connected_inline_mcp_tool_is_listed_once(monkeypatch):
    """The live MCP index enriches inline schemas with a server badge; it
    must replace, not duplicate, the same row returned by spec.build()."""
    import threading
    import lib.mcp as mcp

    schema = {
        'type': 'function',
        'function': {
            'name': 'mcp__wiki__search',
            'description': 'Search the connected wiki',
            'parameters': {'type': 'object', 'properties': {}},
        },
    }

    class _Bridge:
        connected = True
        server_count = 1
        _lock = threading.RLock()
        _tool_index = {
            'mcp__wiki__search': {
                'server_name': 'wiki',
                'tool_name': 'search',
                'openai_def': schema,
            },
        }

        def get_openai_tool_defs(self):
            return [schema]

        def get_tool_safety(self):
            return {'mcp__wiki__search': True}

        def _disabled_tools_for(self, _server):
            return set()

        def list_servers(self):
            return [{'name': 'wiki', 'tools_count': 1}]

    bridge = _Bridge()
    monkeypatch.setattr(mcp, 'get_bridge', lambda: bridge)
    monkeypatch.setenv('TOFU_MCP_TOOL_EXPOSURE', 'inline')

    fam = next(f for (_g, key), f in _families(_inventory()).items()
               if key == 'mcp')
    names = [t['name'] for t in fam['tools'] + fam['mcp_tools']]
    assert names.count('mcp__wiki__search') == 1
    row = next(t for t in fam['mcp_tools']
               if t['name'] == 'mcp__wiki__search')
    assert row['server'] == 'wiki'
    assert fam['counts']['total'] == len(names)


# ── capabilities derivation (the drift repair) ──────────────────────────

def test_capabilities_tools_summary_derives_from_registry():
    from routes.api_v1.capabilities import _tools_summary
    rows = _tools_summary()
    names = {r['name'] for r in rows}
    # The pre-2026-08-06 hand-maintained list missed all of these.
    for expected in ('web_search', 'read_files', 'create_memory',
                     'schedule_create', 'todo_write', 'spawn_agents'):
        assert expected in names, (
            f'{expected} missing from capabilities tools summary — the '
            'derivation from the registry inventory regressed'
        )
    assert all(r['group'] for r in rows), 'every row must carry its group'


# ── Route registration ──────────────────────────────────────────────────

def test_tools_route_registered_on_blueprint():
    from routes.api_v1 import ALL_V1_BLUEPRINTS
    from routes.api_v1.tools import api_v1_tools_bp
    assert api_v1_tools_bp in ALL_V1_BLUEPRINTS


def test_tools_route_serves_payload_via_test_client():
    """End-to-end: the route returns the api_ok envelope with the inventory."""
    import asyncio
    from quart import Quart
    from routes.api_v1.tools import api_v1_tools_bp
    app = Quart(__name__)
    app.register_blueprint(api_v1_tools_bp)

    async def _hit():
        async with app.test_client() as client:
            return await client.get('/api/v1/tools')

    resp = asyncio.run(_hit())
    # require_auth may 401 in a bare app (no cookie/token) — accept either
    # a full payload (auth disabled in this deployment) or the auth gate;
    # what must NEVER happen is a 404/500 (route missing / builder crash).
    assert resp.status_code in (200, 401, 403)
    if resp.status_code == 200:
        body = asyncio.run(resp.get_json())
        data = body.get('data', body)
        assert data['groups'] and data['totals']['tools'] > 0
        assert data['scope'] == 'global_registry'
        assert resp.headers['Cache-Control'] == 'private, no-store'
