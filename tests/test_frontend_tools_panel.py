#!/usr/bin/env python3
"""Guards for Settings → Tools (global live-registry catalogue).

The panel is a process-global catalogue, not a per-conversation capability
switchboard. It renders every family/tool returned by the backend, localizes
all built-ins, and falls back to server-authored descriptions for dynamic
plugin/MCP tools. Gate state may remain in the diagnostic API payload but must
not affect this UI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
PANEL_HTML = os.path.join(ROOT, 'static', 'settings_panels', 'tools.html')
PANEL_MODULE = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'settings', 'tools-inventory.ts')
PANEL_JS = native_module_path('settings/tools-inventory.js', PANEL_MODULE)

_BODY = r'''
const fs = require('fs');
global.window = global;
const elements = {
  toolsInvBody: {
    innerHTML: '', attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
  },
  toolsInvTotalCount: { textContent: '' },
};
global.document = {
  getElementById(id) { return elements[id] || null; },
};
global.t = function (key, vars) {
  const dict = {
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
};
global.escapeHtml = function (value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
};
global.debugLog = function () {};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const results = [];
function check(name, passed) { results.push({ name, passed: !!passed }); }

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
  var html = renderFamily(browserFam, '');
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
  var plugHtml = renderFamily(plugFam, '');
  check('plugin_badge', plugHtml.indexOf('tools-inv-badge is-plugin') !== -1
    && plugHtml.indexOf('acme_kb') !== -1);
  check('dynamic_description_fallback', plugHtml.indexOf('Knowledge base plugin') !== -1
    && plugHtml.indexOf('Search the company knowledge base') !== -1
    && plugHtml.indexOf('toolsInv.family.kb') === -1);

  var mcpHtml = renderToolRow({
    name: 'mcp__wiki__edit', description: 'Edit a wiki page', required: [],
    write: true, enabled: false, server: 'wiki',
  }, true);
  check('mcp_server_badge', mcpHtml.indexOf('tools-inv-badge is-mcp') !== -1
    && mcpHtml.indexOf('wiki') !== -1);
  check('mcp_state_not_rendered', mcpHtml.indexOf('is-disabled') === -1
    && mcpHtml.indexOf('is-off') === -1);

  // Search includes raw backend text AND localized family/tool descriptions.
  check('query_matches_tool_name', familyVisible(browserFam, 'read_page'));
  check('query_matches_localized_tool_desc', familyVisible(browserFam, '点击网页'));
  check('query_matches_localized_family_desc', familyVisible(browserFam, '表单'));
  check('query_miss_hides', !familyVisible(browserFam, 'zzzzz'));

  // Known group order is stable; new backend categories append instead of
  // disappearing behind a frontend whitelist.
  var groups = [
    { id: 'zz_custom_new', families: [] },
    { id: 'project', families: [] },
    { id: 'search', families: [] },
  ];
  var ordered = orderedGroups(groups).map(function (g) { return g.id; });
  check('group_order', ordered.join(',') === 'search,project,zz_custom_new');
  check('unknown_group_title_falls_back', groupTitle('zz_custom_new') === 'zz_custom_new');
  check('known_group_title_i18n', groupTitle('search') === '搜索与抓取');

  var memoryFam = {
    key: 'memory', source: 'builtin', description: 'Memory CRUD tools',
    tools: [
      { name: 'create_memory', description: 'Create', required: ['name'], write: true },
      { name: 'search_memories', description: 'Search', required: [], write: false },
    ], mcp_tools: [],
  };
  var snapshot = {
    scope: 'global_registry', generated_at: 'x',
    totals: { families: 2, tools: 4, active: 0 },
    groups: [
      { id: 'search', families: [memoryFam] },
      { id: 'browser', families: [browserFam] },
    ],
  };
  renderToolsInventory(snapshot, '');
  var body = document.getElementById('toolsInvBody').innerHTML;
  check('full_render_groups', body.indexOf('tools-inv-group-title') !== -1);
  check('full_render_both_families', body.indexOf('>memory<') !== -1 && body.indexOf('>browser<') !== -1);
  check('header_total', document.getElementById('toolsInvTotalCount').textContent === '4 个工具');
  check('all_backend_tools_rendered', (body.match(/tools-inv-tool-name/g) || []).length === 4);
  renderToolsInventory(snapshot, 'zzzz');
  check('search_miss_empty_state', document.getElementById('toolsInvBody').innerHTML.indexOf('没有匹配的工具') !== -1);
} catch (e) {
  check('harness_threw: ' + (e && e.message), false);
}
const failed = results.filter((result) => !result.passed);
console.log(JSON.stringify({ results, failed }));
if (failed.length) process.exitCode = 1;
'''


def test_tools_panel_render_pins():
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    proc = subprocess.run(
        [node, '-e', _BODY, PANEL_JS], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (proc.stdout or '') + (proc.stderr or '')
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not result['failed']
    assert len(result['results']) == 22


def test_tools_panel_header_is_global_catalogue_not_state_filter():
    with open(PANEL_HTML, encoding='utf-8') as fh:
        html = fh.read()
    with open(PANEL_MODULE, encoding='utf-8') as fh:
        js = fh.read()

    assert 'id="settingsTab_tools"' in html
    i_title = html.find('mcp-store-header-title')
    i_refresh = html.find('populateToolsInventory()')
    i_search = html.find('id="toolsInvSearch"')
    assert -1 < i_title < i_refresh < i_search
    assert 'id="toolsInvTotalCount"' in html
    assert 'toolsInv.intro' in html

    forbidden = (
        'toolsInvActiveCount', 'data-tools-filter', '_toolsInvSetFilter',
        'tools-inv-state', 'tools-inv-gate', 'toolsInv.disabledBadge',
        'toolsInv.stateOn', 'toolsInv.gateLabel', '_populateToolsTab',
        '_toolsInvSearch', 'tools_panel.js',
    )
    for token in forbidden:
        assert token not in html + js, f'per-conversation state UI leaked back: {token}'
    assert 'inventoryRequestSequence' in js, (
        'stale refreshes could overwrite newer snapshots')


def test_tools_panel_is_owned_by_the_lazy_settings_domain():
    root = Path(ROOT)
    runtime = (root / 'frontend/src/runtime/app-runtime.js').read_text()
    settings = (root / 'frontend/src/features/settings.ts').read_text()
    main = (root / 'frontend/src/main.ts').read_text()
    misc = (root / 'frontend/src/features/misc.ts').read_text()

    assert 'migrated source: tools_panel.js' not in runtime
    assert "import './settings/tools-inventory'" in settings
    assert "'populateToolsInventory', 'searchToolsInventory'" in main
    assert 'tools_panel.js' not in misc


def test_every_builtin_family_and_tool_has_bilingual_catalogue_copy():
    """A backend-added built-in may render via fallback, but CI must require
    intentional zh/en catalogue copy before it ships."""
    from lib.tools.registry import all_specs

    locales = [
        json.loads((Path(ROOT) / f'frontend/src/i18n/locales/{lang}.json').read_text())
        for lang in ('zh', 'en')
    ]

    missing = []
    for spec in all_specs():
        if spec.source != 'builtin':
            continue
        keys = [f'toolsInv.family.{spec.key}']
        keys.extend(f'toolsInv.tool.{name}' for name in sorted(spec.provides))
        for key in keys:
            if any(not isinstance(locale.get(key), str) or not locale[key]
                   for locale in locales):
                missing.append(key)
    assert not missing, f'built-in tool catalogue i18n missing: {missing}'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
