"""Static product-contract guards for Settings → My Context."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path, runtime_section

pytestmark = pytest.mark.unit
_AUDIT_SYNTHETIC_REPO_PATHS = {'static/js/preferences.js'}

ROOT = Path(__file__).resolve().parents[1]
PREFERENCES_SOURCE = ROOT / 'frontend/src/features/memory/preferences.ts'
PREFERENCES_JS = native_module_path(
    'preferences.js', PREFERENCES_SOURCE)


def _read(relative: str) -> str:
    if relative == 'static/js/preferences.js':
        return PREFERENCES_SOURCE.read_text(encoding='utf-8')
    if relative.startswith('static/js/'):
        return runtime_section(relative.removeprefix('static/js/'))
    return (ROOT / relative).read_text(encoding='utf-8')


def test_panel_explains_always_on_context_and_separate_memory():
    panel = _read('static/settings_panels/preferences.html')
    assert 'data-i18n="context.title"' in panel
    assert 'data-i18n="context.alwaysOn"' in panel
    assert 'id="prefsList"' in panel
    assert 'data-i18n="context.memoryTitle"' in panel
    assert 'clearLegacyMemories(this)' in panel


def test_editor_has_three_explicit_types_and_condition_action_fields():
    source = _read('static/js/preferences.js')
    icons = _read('static/js/core/icons.js')
    for item_type in ('identity', 'work_rule', 'response_preference'):
        assert f"type: '{item_type}'" in source
    assert 'item.condition' in source and 'item.action' in source
    assert 'contextApi().replace(cleanItems())' in source
    assert 'estimateChars() > capacity' in source
    for icon in ('brain', 'wrench', 'messageCircle', 'edit', 'trash'):
        assert f'{icon}:' in icons


def test_context_and_clear_api_wiring_is_complete():
    api = _read('static/js/api.js')
    route = _read('routes/api_v1/memory.py')
    for endpoint in (
        '/api/v1/context',
        '/api/v1/context/changes/${encodeURIComponent(changeId)}/undo',
        '/api/v1/memory/actions/clear',
    ):
        assert endpoint in api
    assert "data.get('confirm') is not True" in route
    assert 'Bulk memory clearing is disabled in multi-user mode' in route


def test_context_layout_has_mobile_contract_and_undo_affordance():
    css = _read('static/settings.css')
    events = _read('static/js/ui/sse_handlers_misc.js')
    timeline = _read('static/js/ui/tool_rounds.js')
    assert '.ctx-group-work_rule' in css
    assert '@media(max-width:760px)' in css
    # My Context widens its panel while active and keeps a content-width
    # (not viewport) single-column floor for the masonry.
    assert '.settings-panel:has(#settingsTab_preferences.active)' in css
    assert '@container' in css
    assert 'Api.userContext.undo(changeId)' in events
    assert 'pl-seg-undo' in timeline


_JSDOM_BODY = r'''
const fs = require('fs');
const path = require('path');
const { setup } = require(process.env.JSDOM_HARNESS);
const root = process.argv[3];
const panel = fs.readFileSync(
  path.join(root, 'static/settings_panels/preferences.html'), 'utf8');
const saved = [];
let cleared = 0;
const sample = [
  { id: 'ctx_a', type: 'identity', text: 'Works at Meituan', source: 'manual' },
  { id: 'ctx_b', type: 'work_rule', condition: 'submitting cluster jobs',
    action: 'use hope MCP', source: 'assistant' },
  { id: 'ctx_c', type: 'response_preference', text: 'Reply in Chinese',
    source: 'legacy_migration' },
];
const { document, check, report } = setup({
  root,
  html: '<!DOCTYPE html><body>' + panel + '</body>',
  targets: [process.argv[2]],
  globals: {
    Api: {
      userContext: {
        get: async () => ({ items: sample, cap: 2500, chars: 152 }),
        replace: async (items) => {
          saved.push(items);
          return { saved: true, items, cap: 2500 };
        },
      },
      memory: {
        clearPreview: async () => ({ total: 2, global: 1, project: 1 }),
        clearAll: async () => { cleared += 1; return {
          deleted_ids: ['a', 'b'], failed_ids: [] }; },
      },
    },
    showConfirm: async () => true,
    refreshMemoryList: () => {},
    debugLog: () => {},
  },
});

(async () => {
  try {
    await refreshPreferences();
    check('three_category_cards', document.querySelectorAll('.ctx-group').length === 3);
    check('identity_rendered', document.body.textContent.includes('Works at Meituan'));
    check('rule_condition_rendered', document.body.textContent.includes('submitting cluster jobs'));
    check('rule_action_rendered', document.body.textContent.includes('use hope MCP'));
    check('response_preference_rendered', document.body.textContent.includes('Reply in Chinese'));
    check('category_icons_visible', document.querySelectorAll('.ctx-group-icon svg').length === 3);
    check('initially_clean', document.getElementById('ctxSaveBtn').disabled === true);

    _contextUpdateField(0, 'text', 'Works at Meituan as an engineer');
    check('edit_marks_dirty', document.getElementById('ctxDirtyState').textContent.length > 0);
    check('valid_edit_enables_save', document.getElementById('ctxSaveBtn').disabled === false);
    await savePreferences(document.getElementById('ctxSaveBtn'));
    check('structured_replace_called', saved.length === 1 &&
      saved[0][0].type === 'identity' && saved[0][0].source === 'manual');

    await clearLegacyMemories(document.querySelector('[onclick^="clearLegacyMemories"]'));
    check('confirmed_clear_called_once', cleared === 1);
  } catch (error) {
    check('unexpected_exception_' + error.message, false);
  }
  report();
})();
'''


def test_real_editor_renders_edits_saves_and_clears_under_jsdom():
    run_harness(
        target_js=PREFERENCES_JS,
        body_js=_JSDOM_BODY,
        expect_pass=11,
        label='My Context editor',
    )
