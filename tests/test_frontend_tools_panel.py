#!/usr/bin/env python3
"""Guards for Settings → Tools (global live-registry catalogue).

The panel is a process-global catalogue, not a per-conversation capability
switchboard. It renders every family/tool returned by the backend, localizes
all built-ins, and falls back to server-authored descriptions for dynamic
plugin/MCP tools. Gate state may remain in the diagnostic API payload but must
not affect this UI.
"""

from __future__ import annotations

import os
import re

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
PANEL_HTML = os.path.join(ROOT, 'static', 'settings_panels', 'tools.html')
PANEL_JS = os.path.join(JS_DIR, 'tools_panel.js')
I18N_JS = os.path.join(JS_DIR, 'i18n.js')

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<div id="toolsInvBody"></div>' +
    '<span id="toolsInvTotalCount"></span>' +
    '</body>',
  targets: [process.argv[2]],
  globals: {
    t: function (key, vars) {
      var dict = {
        'toolsInv.writeBadge': '写',
        'toolsInv.writeTitle': '写工具',
        'toolsInv.pluginBadge': '插件',
        'toolsInv.required': '必填参数:',
        'toolsInv.familyEmpty': '当前未注册具体工具。',
        'toolsInv.noMatch': '没有匹配的工具。',
        'toolsInv.countTotal': (vars && vars.n) + ' 个工具',
        'toolsInv.familyCount': (vars && vars.n) + ' 个',
        'toolsInv.group.search': '搜索与抓取',
        'toolsInv.family.browser': '操作浏览器标签页、页面元素和表单。',
        'toolsInv.family.memory': '搜索、创建和整理长期记忆。',
        'toolsInv.tool.browser_click': '点击网页上的元素。',
        'toolsInv.tool.browser_read_page': '读取当前网页的内容。',
        'toolsInv.tool.create_memory': '创建一条新的长期记忆。',
        'toolsInv.tool.search_memories': '搜索已积累的长期记忆。',
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
  // Backend gate fields are deliberately present in this fixture: the panel
  // must ignore them and display the complete global catalogue uniformly.
  var browserFam = {
    key: 'browser', phase: 'base', source: 'builtin', plugin_name: '',
    description: 'Browser automation tools', gate: '连接浏览器扩展',
    gate_state: 'off', gate_reason: 'gate_closed',
    tools: [
      { name: 'browser_click', description: 'Click an element', required: [],
        write: true, handler: true, enabled: false },
      { name: 'browser_read_page', description: '', required: ['tab_id'],
        write: false, handler: true, enabled: false },
    ],
    mcp_tools: [], counts: { active: 0, total: 2 },
  };
  var html = _toolsInvRenderFamily(browserFam, '');
  check('localized_family_description', html.indexOf('操作浏览器标签页') !== -1);
  check('localized_tool_descriptions', html.indexOf('点击网页上的元素') !== -1
    && html.indexOf('读取当前网页') !== -1);
  check('family_total_only', html.indexOf('2 个') !== -1 && html.indexOf('0/2') === -1);
  check('write_badge', html.indexOf('tools-inv-badge is-write') !== -1);
  check('required_params', html.indexOf('必填参数') !== -1 && html.indexOf('tab_id') !== -1);
  check('no_state_ui', html.indexOf('tools-inv-state') === -1
    && html.indexOf('tools-inv-gate') === -1
    && html.indexOf('is-off') === -1
    && html.indexOf('连接浏览器扩展') === -1);

  // Unknown plugin and MCP names use live backend descriptions, never an i18n
  // key. A disabled MCP tool remains in the global catalogue without a state
  // badge or dimming.
  var plugFam = {
    key: 'kb', source: 'plugin', plugin_name: 'acme_kb',
    description: 'Knowledge base plugin', tools: [
      { name: 'acme_search', description: 'Search the company knowledge base',
        required: [], write: false, enabled: false },
    ], mcp_tools: [],
  };
  var plugHtml = _toolsInvRenderFamily(plugFam, '');
  check('plugin_badge', plugHtml.indexOf('tools-inv-badge is-plugin') !== -1
    && plugHtml.indexOf('acme_kb') !== -1);
  check('dynamic_description_fallback', plugHtml.indexOf('Knowledge base plugin') !== -1
    && plugHtml.indexOf('Search the company knowledge base') !== -1
    && plugHtml.indexOf('toolsInv.family.kb') === -1);

  var mcpHtml = _toolsInvRenderToolRow({
    name: 'mcp__wiki__edit', description: 'Edit a wiki page', required: [],
    write: true, enabled: false, server: 'wiki',
  }, true);
  check('mcp_server_badge', mcpHtml.indexOf('tools-inv-badge is-mcp') !== -1
    && mcpHtml.indexOf('wiki') !== -1);
  check('mcp_state_not_rendered', mcpHtml.indexOf('is-disabled') === -1
    && mcpHtml.indexOf('is-off') === -1);

  // Search includes raw backend text AND localized family/tool descriptions.
  check('query_matches_tool_name', _toolsInvFamilyVisible(browserFam, 'read_page'));
  check('query_matches_localized_tool_desc', _toolsInvFamilyVisible(browserFam, '点击网页'));
  check('query_matches_localized_family_desc', _toolsInvFamilyVisible(browserFam, '表单'));
  check('query_miss_hides', !_toolsInvFamilyVisible(browserFam, 'zzzzz'));

  // Known group order is stable; new backend categories append instead of
  // disappearing behind a frontend whitelist.
  var groups = [
    { id: 'zz_custom_new', families: [] },
    { id: 'project', families: [] },
    { id: 'search', families: [] },
  ];
  var ordered = _toolsInvOrderedGroups(groups).map(function (g) { return g.id; });
  check('group_order', ordered.join(',') === 'search,project,zz_custom_new');
  check('unknown_group_title_falls_back', _toolsInvGroupTitle('zz_custom_new') === 'zz_custom_new');
  check('known_group_title_i18n', _toolsInvGroupTitle('search') === '搜索与抓取');

  var memoryFam = {
    key: 'memory', source: 'builtin', description: 'Memory CRUD tools',
    tools: [
      { name: 'create_memory', description: 'Create', required: ['name'], write: true },
      { name: 'search_memories', description: 'Search', required: [], write: false },
    ], mcp_tools: [],
  };
  _toolsInvData = {
    scope: 'global_registry', generated_at: 'x',
    totals: { families: 2, tools: 4, active: 0 },
    groups: [
      { id: 'search', families: [memoryFam] },
      { id: 'browser', families: [browserFam] },
    ],
  };
  _toolsInvQuery = '';
  _toolsInvRender();
  var body = document.getElementById('toolsInvBody').innerHTML;
  check('full_render_groups', body.indexOf('tools-inv-group-title') !== -1);
  check('full_render_both_families', body.indexOf('>memory<') !== -1 && body.indexOf('>browser<') !== -1);
  check('header_total', document.getElementById('toolsInvTotalCount').textContent === '4 个工具');
  check('all_backend_tools_rendered', (body.match(/tools-inv-tool-name/g) || []).length === 4);
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
        expect_pass=22,
        label='tools-panel',
    )


def test_tools_panel_header_is_global_catalogue_not_state_filter():
    with open(PANEL_HTML, encoding='utf-8') as fh:
        html = fh.read()
    with open(PANEL_JS, encoding='utf-8') as fh:
        js = fh.read()

    assert 'id="settingsTab_tools"' in html
    i_title = html.find('mcp-store-header-title')
    i_refresh = html.find('_populateToolsTab()')
    i_search = html.find('id="toolsInvSearch"')
    assert -1 < i_title < i_refresh < i_search
    assert 'id="toolsInvTotalCount"' in html
    assert 'toolsInv.intro' in html

    forbidden = (
        'toolsInvActiveCount', 'data-tools-filter', '_toolsInvSetFilter',
        'tools-inv-state', 'tools-inv-gate', 'toolsInv.disabledBadge',
        'toolsInv.stateOn', 'toolsInv.gateLabel',
    )
    for token in forbidden:
        assert token not in html + js, f'per-conversation state UI leaked back: {token}'
    assert '_toolsInvRequestSeq' in js, 'stale refreshes could overwrite newer snapshots'


def test_every_builtin_family_and_tool_has_bilingual_catalogue_copy():
    """A backend-added built-in may render via fallback, but CI must require
    intentional zh/en catalogue copy before it ships."""
    from lib.tools import all_specs

    with open(I18N_JS, encoding='utf-8') as fh:
        source = fh.read()

    missing = []
    for spec in all_specs():
        if spec.source != 'builtin':
            continue
        keys = [f'toolsInv.family.{spec.key}']
        keys.extend(f'toolsInv.tool.{name}' for name in sorted(spec.provides))
        for key in keys:
            pattern = re.compile(
                r"['\"]" + re.escape(key) +
                r"['\"]\s*:\s*\{\s*zh\s*:\s*.+?,\s*en\s*:\s*.+?\s*\}",
            )
            if not pattern.search(source):
                missing.append(key)
    assert not missing, f'built-in tool catalogue i18n missing: {missing}'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
