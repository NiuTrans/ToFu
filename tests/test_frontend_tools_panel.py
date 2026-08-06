#!/usr/bin/env python3
"""Guards for the Settings → 工具 panel (live tool-registry inventory).

Born 2026-08-06 (owner directive: "show me every registered tool, organized
by group, in real time"). Three layers pinned:

  1. jsdom render pins — family card renders the state pill, the gate line
     (how to enable) for gated-off families, tool rows with write/disabled
     badges, plugin badge; the header counts and filter pills.
  2. filter/search logic — _toolsInvFamilyVisible (all/on/off + query) and
     the unknown-category ordering fallback (a new spec category must never
     be silently hidden — the MCP panel lost two whole categories to a
     literal whitelist once; the skills panel comment documents that class).
  3. panel-structure pins — header control order + the SETTINGS_PANEL
     fragment contract (parity suite covers the marker loop).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
     tests/test_frontend_tools_panel.py
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
PANEL_HTML = os.path.join(ROOT, 'static', 'settings_panels', 'tools.html')
PANEL_JS = os.path.join(JS_DIR, 'tools_panel.js')

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<div id="toolsInvBody"></div>' +
    '<span id="toolsInvTotalCount"></span><span id="toolsInvActiveCount"></span>' +
    '</body>',
  targets: [process.argv[2]],
  globals: {
    t: function (key, vars) {
      var dict = {
        'toolsInv.stateOn': '启用', 'toolsInv.stateOff': '未启用',
        'toolsInv.stateStandby': '待机', 'toolsInv.stateError': '异常',
        'toolsInv.gateLabel': '开启方式：',
        'toolsInv.writeBadge': '写',
        'toolsInv.writeTitle': '写工具',
        'toolsInv.disabledBadge': '已禁用',
        'toolsInv.pluginBadge': '插件',
        'toolsInv.required': '必填参数:',
        'toolsInv.familyEmpty': '（空）',
        'toolsInv.noMatch': '没有匹配的工具。',
        'toolsInv.countTotal': (vars && vars.n) + ' 个工具',
        'toolsInv.countActive': (vars && vars.n) + ' 个启用',
        'toolsInv.group.search': '搜索与抓取',
      };
      return dict[key] !== undefined ? dict[key] : key;
    },
    escapeHtml: function (s) {
      return String(s === undefined || s === null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },
    debugLog: function () {},
  },
});

try {
  // ── 1. Gated-off family: state pill + gate line + disabled rows ──
  var fam = {
    key: 'browser', phase: 'base', source: 'builtin', plugin_name: '',
    description: 'Browser automation tools',
    gate: '安装并连接浏览器扩展（设置 → 网络）',
    gate_state: 'off', gate_reason: 'gate_closed',
    tools: [
      { name: 'browser_click', description: 'Click an element', required: [],
        write: true, handler: true, enabled: false },
      { name: 'browser_read_page', description: 'Read the page', required: ['tab_id'],
        write: false, handler: true, enabled: false },
    ],
    mcp_tools: [], counts: { active: 0, total: 2 },
  };
  var html = _toolsInvRenderFamily(fam, '');
  check('off_state_pill', html.indexOf('tools-inv-state is-off') !== -1 && html.indexOf('未启用') !== -1);
  check('gate_line_shown_when_off', html.indexOf('tools-inv-gate') !== -1
    && html.indexOf('开启方式') !== -1 && html.indexOf('浏览器扩展') !== -1);
  check('family_count', html.indexOf('0/2') !== -1);
  check('write_badge', html.indexOf('tools-inv-badge is-write') !== -1);
  check('disabled_rows', (html.match(/tools-inv-tool is-off/g) || []).length === 2);
  check('required_params', html.indexOf('必填参数') !== -1 && html.indexOf('tab_id') !== -1);
  check('no_gate_line_when_empty_gate', _toolsInvRenderFamily({
    key: 'x', gate: '', gate_state: 'off', tools: [], mcp_tools: [],
    counts: { active: 0, total: 0 }, source: 'builtin', description: '',
  }, '').indexOf('tools-inv-gate"') === -1);

  // ── 2. On family: no gate line, on pill, enabled rows ──
  var onFam = {
    key: 'memory', phase: 'capability', source: 'builtin', plugin_name: '',
    description: 'Memory CRUD tools', gate: '常开',
    gate_state: 'on', gate_reason: '',
    tools: [
      { name: 'create_memory', description: 'Create', required: ['name'],
        write: true, handler: true, enabled: true },
      { name: 'search_memories', description: 'Search', required: [],
        write: false, handler: true, enabled: true },
    ],
    mcp_tools: [], counts: { active: 2, total: 2 },
  };
  var onHtml = _toolsInvRenderFamily(onFam, '');
  check('on_state_pill', onHtml.indexOf('tools-inv-state is-on') !== -1 && onHtml.indexOf('启用') !== -1);
  check('on_family_no_gate_line', onHtml.indexOf('tools-inv-gate') === -1);
  check('on_rows_not_dimmed', onHtml.indexOf('tools-inv-tool is-off') === -1);
  check('read_tool_no_write_badge', (function () {
    var rows = onHtml.split('tools-inv-tool-name');
    var searchRow = rows[2] || '';
    return searchRow.indexOf('search_memories') !== -1;
  })());

  // ── 3. Plugin family badge ──
  var plugFam = {
    key: 'kb', phase: 'base', source: 'plugin', plugin_name: 'acme_kb',
    description: 'KB plugin', gate: '', gate_state: 'off',
    gate_reason: 'plugin_not_allowlisted',
    tools: [{ name: 'acme_search', description: '', required: [],
              write: false, handler: true, enabled: false }],
    mcp_tools: [], counts: { active: 0, total: 1 },
  };
  var plugHtml = _toolsInvRenderFamily(plugFam, '');
  check('plugin_badge', plugHtml.indexOf('tools-inv-badge is-plugin') !== -1
    && plugHtml.indexOf('acme_kb') !== -1);

  // ── 4. MCP rows carry the server badge + disabled state ──
  var mcpFam = {
    key: 'mcp', phase: 'capability', source: 'builtin', plugin_name: '',
    description: 'External MCP-server tools', gate: '设置 → MCP',
    gate_state: 'standby', gate_reason: 'no_server_connected',
    tools: [],
    mcp_tools: [
      { name: 'mcp__wiki__search', description: 'Wiki search', required: [],
        write: false, handler: true, enabled: true, server: 'wiki' },
      { name: 'mcp__wiki__edit', description: 'Wiki edit', required: [],
        write: true, handler: true, enabled: false, server: 'wiki' },
    ],
    mcp_servers: [{ name: 'wiki', tools_count: 2 }],
    counts: { active: 1, total: 2 },
  };
  var mcpHtml = _toolsInvRenderFamily(mcpFam, '');
  check('standby_pill', mcpHtml.indexOf('tools-inv-state is-standby') !== -1 && mcpHtml.indexOf('待机') !== -1);
  check('mcp_server_badge', (mcpHtml.match(/tools-inv-badge is-mcp/g) || []).length === 2);
  check('mcp_disabled_badge', mcpHtml.indexOf('tools-inv-badge is-disabled') !== -1);

  // ── 5. Filter + search semantics ──
  check('filter_on_hides_all_off_family', !_toolsInvFamilyVisible(fam, 'on', ''));
  check('filter_on_keeps_on_family', _toolsInvFamilyVisible(onFam, 'on', ''));
  check('filter_off_hides_on_family', !_toolsInvFamilyVisible(onFam, 'off', ''));
  check('query_matches_tool_name', _toolsInvFamilyVisible(fam, 'all', 'read_page'));
  check('query_matches_family_key', _toolsInvFamilyVisible(fam, 'all', 'brows'));
  check('query_miss_hides', !_toolsInvFamilyVisible(fam, 'all', 'zzzzz'));

  // ── 6. Group ordering: known order, unknown appended (never hidden) ──
  var groups = [
    { id: 'zz_custom_new', families: [] },
    { id: 'project', families: [] },
    { id: 'search', families: [] },
  ];
  var ordered = _toolsInvOrderedGroups(groups).map(function (g) { return g.id; });
  check('group_order', ordered.join(',') === 'search,project,zz_custom_new');
  check('unknown_group_title_falls_back', _toolsInvGroupTitle('zz_custom_new') === 'zz_custom_new');
  check('known_group_title_i18n', _toolsInvGroupTitle('search') === '搜索与抓取');

  // ── 7. Full render: header counts + body markup ──
  _toolsInvData = {
    generated_at: 'x', reference: {}, installed_plugins: [],
    totals: { families: 2, tools: 4, active: 2 },
    groups: [
      { id: 'search', families: [onFam] },
      { id: 'browser', families: [fam] },
    ],
  };
  _toolsInvFilter = 'all'; _toolsInvQuery = '';
  _toolsInvRender();
  var body = document.getElementById('toolsInvBody').innerHTML;
  check('full_render_groups', body.indexOf('tools-inv-group-title') !== -1);
  check('full_render_both_families', body.indexOf('>memory<') !== -1 && body.indexOf('>browser<') !== -1);
  check('header_counts', document.getElementById('toolsInvTotalCount').textContent === '4 个工具'
    && document.getElementById('toolsInvActiveCount').textContent === '2 个启用');
  _toolsInvQuery = 'zzzz';
  _toolsInvRender();
  check('search_miss_empty_state', document.getElementById('toolsInvBody').innerHTML.indexOf('没有匹配的工具') !== -1);
} catch (e) {
  check('harness_threw: ' + (e && e.message), false);
}
report();
'''


def test_tools_panel_render_pins():
    run_harness(
        target_js=PANEL_JS,
        body_js=_BODY,
        expect_pass=28,
        label='tools-panel',
    )


# ── Panel-structure pins (the fragment contract) ──────────────────────

def test_tools_panel_header_structure():
    with open(PANEL_HTML, encoding='utf-8') as fh:
        html = fh.read()

    assert 'id="settingsTab_tools"' in html, 'fragment must define its own panel'
    # Header control order: title block → filter pills → refresh → search.
    i_title = html.find('mcp-store-header-title')
    i_pills = html.find('data-tools-filter="all"')
    i_refresh = html.find('_populateToolsTab()')
    i_search = html.find('id="toolsInvSearch"')
    assert -1 < i_title < i_pills < i_refresh < i_search, (
        'tools.html header control order drifted (title | pills | refresh | '
        'search — the same order language as the MCP/Skills store headers)'
    )
    # The count badges live inside the title block.
    title_block = html[i_title:i_pills]
    assert 'id="toolsInvTotalCount"' in title_block and \
        'id="toolsInvActiveCount"' in title_block
    # Static onclicks covered by feature-loader stubs.
    assert "_toolsInvSetFilter('all')" in html and '_toolsInvSearch(' in html
    # The intro paragraph explains the read-only live inventory contract.
    assert 'toolsInv.intro' in html


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
