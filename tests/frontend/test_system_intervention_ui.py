"""System intervention blocks render action facts and untrusted text safely."""
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_system_interventions_are_collapsed_safe_and_action_specific():
    target = native_module_path(
        '.native/system-intervention-renderer.js',
        ROOT / 'frontend/src/conversation/ui/classic-conversation-renderers.ts')
    run_harness(target_js=str(target), label='system intervention UI', expect_pass=4,
                body_js=r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({root: process.argv[3], targets: [process.argv[2]]});
const renderers = createClassicConversationRenderers({renderSafeMarkdownHtml: () => {throw Error('intervention must not reach markdown');}});
const node = document.createElement('div');
const block = {kind: 'system-note', noteKind: 'system-intervention', action: 'stop',
  reason: '<img src=x onerror=bad>', text: '<script>bad()</script> **raw**'};
renderers.renderBlock(node, block, {turn: {actor: 'assistant'}});
check('collapsed_by_default', !!node.querySelector('details') && !node.querySelector('details').open);
check('stop_not_injection', node.textContent.includes('Stopped') && !node.textContent.includes('Context appended'));
check('raw_text_safe', !node.querySelector('script,img') && node.querySelector('pre').textContent === block.text
  && node.textContent.includes(block.reason));
renderers.renderBlock(node, {...block, action: 'prompt'}, {turn: {actor: 'assistant'}});
check('rerender_updates_action_without_duplicates', node.textContent.includes('Context appended')
  && !node.textContent.includes('Stopped') && node.querySelectorAll('details').length === 1);
report();
""")


def test_active_checklist_note_gets_first_class_title_icon_and_flag():
    """reason=active_checklist must surface as its own titled intervention,
    not stay buried in the card's reason line."""
    target = native_module_path(
        '.native/system-intervention-renderer.js',
        ROOT / 'frontend/src/conversation/ui/classic-conversation-renderers.ts')
    run_harness(target_js=str(target), label='active checklist note UI', expect_pass=6,
                body_js=r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({root: process.argv[3], targets: [process.argv[2]]});
/* Resolve labels through the real i18n authority so the title is the actual
 * localized string, not the port-less 'System note' fallback. */
const renderers = createClassicConversationRenderers({renderSafeMarkdownHtml: () => '',
  localizedText: (key, fb) => ({
    'systemNote.activeChecklistLabel': 'System intervention \u00b7 Active checklist',
    'systemNote.modifiedFilesLabel': 'System intervention \u00b7 Modified files',
    'systemNote.contextInjectionLabel': 'Context injected',
  })[key] || fb});
const node = document.createElement('div');
const block = {kind: 'system-note', noteKind: 'context-injection', action: 'prompt',
  reason: 'active_checklist', text: '## Active Task Checklist\n- [ ] keep going'};
renderers.renderBlock(node, block, {turn: {actor: 'assistant'}});
const details = node.querySelector('details');
const label = node.querySelector('.ptool-text');
check('checklist_flag_on_row', details.dataset.checklist === 'true');
check('title_uses_checklist_label_not_reason',
  label.textContent.includes('Active checklist') && !label.textContent.includes('active_checklist'));
check('task_list_icon_reuses_todo_presentation',
  node.querySelector('.ptool-icon svg') !== null
  && node.querySelector('.ptool-icon').innerHTML.includes('M9 11l3 3L22 4'));
renderers.renderBlock(node, {...block, reason: 'modified_files'}, {turn: {actor: 'assistant'}});
check('modified_files_reason_flag_no_checklist',
  !node.querySelector('details').dataset.checklist
  && node.querySelector('details').dataset.reason === 'modified_files');
check('modified_files_title',
  node.querySelector('.ptool-text').textContent.includes('Modified files'));
check('modified_files_icon',
  node.querySelector('.ptool-icon').innerHTML.includes('M17 3a2.85'));
report();
""")
