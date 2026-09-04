"""Exact owner and delegated-action contract for Human Guidance presentation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OWNER_JS = Path(native_module_path(
    '.native/tool-human-guidance-presentation-contract.js',
    ROOT
    / 'frontend/src/conversation/presentation/'
    / 'tool-human-guidance-presentation.ts',
))
ACTION_REGISTRY_JS = Path(native_module_path(
    '.native/action-registry-human-guidance-contract.js',
    ROOT / 'frontend/src/action-registry.ts',
))


_OWNER_HARNESS = r"""
const messages = {
  'project.hgAnswered': 'answered-local',
  'project.hgExpired': 'expired-local',
  'project.hgPanelTitle': 'panel-local',
  'project.hgSubmit': 'submit-local',
  'project.hgTextareaPlaceholder': 'placeholder-local',
  'project.hgTranslatingQuestion': 'translating-local',
  'project.hgUnanswered': 'unanswered-local',
  'project.hgWaitingContinue': 'waiting-local',
  'project.hgWaitingReply': 'reply-local',
  'toolHumanGuidance.contentLimit': 'content-limit:{n}',
  'toolHumanGuidance.defaultQuestion': 'default-question-local',
  'toolHumanGuidance.identifierLimit': 'identifier-limit:{n}',
  'toolHumanGuidance.label': 'guidance-local',
  'toolHumanGuidance.optionFallback': 'option-local:{index}',
  'toolHumanGuidance.optionsLimit': 'options-limit:{shown}/{total}',
};
function translate(key, params) {
  let value = messages[key] || key;
  if (!params || typeof params !== 'object') return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name]) : token
  ));
}
function escaped(value) {
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
const markdownInputs = [];
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2], process.argv[4]],
});
const presentation = createToolHumanGuidancePresentation({
  translate,
  renderMarkdown: (source) => {
    markdownInputs.push(source);
    return `<md>${escaped(source)}</md>`;
  },
});
function occurrences(value, fragment) {
  return String(value).split(fragment).length - 1;
}

const limits = TOOL_HUMAN_GUIDANCE_PRESENTATION_LIMITS;
check('limits_and_port_are_frozen_and_narrow',
  Object.isFrozen(limits)
  && limits.identifierUnits === 512
  && limits.questionUnits === 32768
  && limits.optionsJsonUnits === 65536
  && limits.optionItems === 16
  && limits.optionLabelUnits === 1024
  && limits.optionDescriptionUnits === 8192
  && limits.skippedPreviewUnits === 60
  && limits.responsePreviewUnits === 80
  && Object.isFrozen(presentation)
  && Object.keys(presentation).length === 1);
check('unrelated_values_fail_closed',
  presentation.renderGuidanceHtml(null, { iconHtml: '' }) === ''
  && presentation.renderGuidanceHtml(
    { status: 'done', toolName: 'read_file' },
    { iconHtml: '' },
  ) === '');

const hostileId = "hg'\"><script>globalThis.pwned=true</script>";
const hostileLabel = "original');globalThis.pwned=true;//";
const choiceRound = Object.freeze({
  status: 'awaiting_human',
  toolName: 'ask_human',
  guidanceId: hostileId,
  guidanceQuestion: '<Question>',
  _translatedQuestion: '**Translated question**',
  guidanceType: 'choice',
  guidanceOptions: Object.freeze([
    Object.freeze({
      label: hostileLabel,
      _translatedLabel: '<Translated label>',
      description: 'original description',
      _translatedDescription: '**Translated description**',
    }),
    Object.freeze({ label: 'Safe second' }),
  ]),
});
const choiceBefore = JSON.stringify(choiceRound);
const choiceHtml = presentation.renderGuidanceHtml(
  choiceRound,
  { iconHtml: '<trusted-icon>', toolDisplayLabel: 'Guidance' },
);
check('live_choice_projects_translated_display_and_original_payload',
  choiceHtml.includes('<md>**Translated question**</md>')
  && choiceHtml.includes('&lt;Translated label&gt;')
  && choiceHtml.includes('<md>**Translated description**</md>')
  && choiceHtml.includes(escaped(hostileLabel)));
check('hostile_values_are_escaped_in_attributes',
  choiceHtml.includes(`data-gid="${escaped(hostileId)}"`)
  && choiceHtml.includes(`data-label="${escaped(hostileLabel)}"`)
  && !choiceHtml.includes('<script>'));
check('delegated_choice_actions_are_static',
  occurrences(
    choiceHtml,
    'event.stopPropagation();submitHumanGuidanceChoice(this)',
  ) === 2
  && occurrences(
    choiceHtml,
    'event.preventDefault();submitHumanGuidanceChoice(this)',
  ) === 2
  && !choiceHtml.includes("submitHumanGuidanceChoice('")
  && !choiceHtml.includes('globalThis.pwned=true;//)'));
check('owner_never_mutates_choice_input', JSON.stringify(choiceRound) === choiceBefore);

check('live_choice_owns_one_note_input_per_option',
  occurrences(choiceHtml, 'class="hg-option-group"') === 2
  && occurrences(choiceHtml, 'class="hg-textarea hg-option-note-input"') === 2
  && choiceHtml.includes('maxlength="4096"')
  && choiceHtml.includes('data-option-index="0"')
  && choiceHtml.includes('data-option-index="1"')
  && choiceHtml.includes('project.hgChoiceNotePlaceholder'));

const expiredHtml = presentation.renderGuidanceHtml(
  { ...choiceRound, _turnSettled: true },
  { iconHtml: '<trusted-icon>', toolDisplayLabel: 'Guidance' },
);
check('expired_choice_is_read_only_with_static_options',
  expiredHtml.includes('hg-expired')
  && occurrences(expiredHtml, 'hg-option-static') === 2
  && !expiredHtml.includes('data-tofu-action')
  && !expiredHtml.includes('hg-option-note-input')
  && !expiredHtml.includes('hg-submit-btn')
  && expiredHtml.includes('expired-local'));

const legacyJsonHtml = presentation.renderGuidanceHtml({
  status: 'awaiting_human',
  guidanceId: 'legacy-json',
  guidanceType: 'choice',
  guidanceOptions: JSON.stringify([{ label: 'legacy-a' }, { label: 'legacy-b' }]),
}, { iconHtml: '' });
check('bounded_legacy_json_options_are_normalized',
  occurrences(legacyJsonHtml, 'class="hg-option-card"') === 2
  && legacyJsonHtml.includes('legacy-a')
  && legacyJsonHtml.includes('legacy-b'));

const freeHtml = presentation.renderGuidanceHtml({
  status: 'awaiting_human',
  guidanceId: hostileId,
  guidanceQuestion: '',
  guidanceType: 'free_text',
  _hgTranslating: true,
}, { iconHtml: '' });
check('free_text_uses_static_dataset_actions_and_localized_fallback',
  freeHtml.includes('default-question-local')
  && freeHtml.includes('translating-local')
  && freeHtml.includes('submitHumanGuidanceFreeText(this)')
  && freeHtml.includes('maxlength="32768"')
  && occurrences(freeHtml, `data-gid="${escaped(hostileId)}"`) === 3
  && !freeHtml.includes("submitHumanGuidanceFreeText('"));

const longQuestionHtml = presentation.renderGuidanceHtml({
  status: 'awaiting_human',
  guidanceId: 'long-question',
  guidanceQuestion: 'Q'.repeat(32768) + 'QUESTION_TAIL',
}, { iconHtml: '' });
check('question_markdown_is_bounded_with_visible_notice',
  longQuestionHtml.includes('content-limit:32768')
  && !longQuestionHtml.includes('QUESTION_TAIL')
  && markdownInputs.at(-1).length === 32769);

const manyOptions = Array.from({ length: 20 }, (_, index) => ({
  label: `option-${index}`,
}));
const manyHtml = presentation.renderGuidanceHtml({
  status: 'awaiting_human',
  guidanceId: 'many',
  guidanceType: 'choice',
  guidanceOptions: manyOptions,
}, { iconHtml: '' });
check('option_count_is_bounded_and_visible',
  occurrences(manyHtml, 'class="hg-option-card"') === 16
  && manyHtml.includes('options-limit:16/20')
  && !manyHtml.includes('option-16'));

const longDescriptionHtml = presentation.renderGuidanceHtml({
  status: 'awaiting_human',
  guidanceId: 'long-description',
  guidanceType: 'choice',
  guidanceOptions: [{
    label: 'label',
    description: 'D'.repeat(8192) + 'DESCRIPTION_TAIL',
  }],
}, { iconHtml: '' });
check('option_markdown_is_bounded_with_accurate_notice',
  longDescriptionHtml.includes('content-limit:8192')
  && !longDescriptionHtml.includes('DESCRIPTION_TAIL')
  && markdownInputs.at(-1).length === 8193);

const longLabelHtml = presentation.renderGuidanceHtml({
  status: 'awaiting_human',
  guidanceId: 'long-label',
  guidanceType: 'choice',
  guidanceOptions: [{
    label: 'L'.repeat(1024) + 'LABEL_TAIL',
  }],
}, { iconHtml: '' });
check('oversized_original_label_cannot_submit_a_truncated_value',
  longLabelHtml.includes('content-limit:1024')
  && longLabelHtml.includes('hg-option-unavailable')
  && !longLabelHtml.includes('data-tofu-action')
  && !longLabelHtml.includes('LABEL_TAIL'));

const tooLongIdHtml = presentation.renderGuidanceHtml({
  status: 'awaiting_human',
  guidanceId: 'I'.repeat(512) + 'ID_TAIL',
  guidanceQuestion: 'Still visible',
}, { iconHtml: '' });
check('oversized_identifier_fails_closed_without_wrong_submission',
  tooLongIdHtml.includes('identifier-limit:512')
  && tooLongIdHtml.includes('hg-unavailable')
  && !tooLongIdHtml.includes('data-tofu-action')
  && !tooLongIdHtml.includes('hg-textarea')
  && !tooLongIdHtml.includes('ID_TAIL'));

let malformedHtml = '';
let malformedThrew = false;
try {
  malformedHtml = presentation.renderGuidanceHtml({
    status: 'awaiting_human',
    guidanceId: 'malformed',
    guidanceType: 'choice',
    guidanceOptions: [null, 7, { label: '<safe>' }],
  }, { iconHtml: '' });
} catch { malformedThrew = true; }
check('malformed_option_items_degrade_without_throwing',
  malformedThrew === false
  && malformedHtml.includes('option-local:1')
  && malformedHtml.includes('option-local:2')
  && malformedHtml.includes('&lt;safe&gt;'));

const skippedHtml = presentation.renderGuidanceHtml({
  status: 'done',
  toolName: 'ask_human',
  _hgSkipped: true,
  guidanceQuestion: '<' + 'S'.repeat(80) + 'SKIP_TAIL',
}, { iconHtml: '<trusted-icon>', toolDisplayLabel: '<Guidance>' });
check('skipped_row_is_bounded_and_escaped',
  skippedHtml.includes('hg-skipped-line')
  && skippedHtml.includes('<trusted-icon>')
  && skippedHtml.includes('&lt;Guidance&gt;')
  && skippedHtml.includes('&lt;' + 'S'.repeat(59) + '…')
  && !skippedHtml.includes('SKIP_TAIL'));

const submittedHtml = presentation.renderGuidanceHtml({
  status: 'submitted',
  toolName: 'ask_human',
  _hgUserResponse: '<' + 'R'.repeat(100) + 'RESPONSE_TAIL',
}, { iconHtml: '<trusted-icon>' });
check('submitted_row_is_bounded_and_localized',
  submittedHtml.includes('hg-submitted-line')
  && submittedHtml.includes('guidance-local')
  && submittedHtml.includes('&lt;' + 'R'.repeat(79) + '…')
  && submittedHtml.includes('answered-local')
  && submittedHtml.includes('waiting-local')
  && !submittedHtml.includes('RESPONSE_TAIL'));

const invalidJsonHtml = presentation.renderGuidanceHtml({
  status: 'awaiting_human',
  guidanceId: 'invalid-json',
  guidanceType: 'choice',
  guidanceOptions: '[' + 'X'.repeat(65536),
}, { iconHtml: '' });
check('oversized_json_is_not_parsed_and_degrades_visibly',
  invalidJsonHtml.includes('hg-freetext-wrap')
  && invalidJsonHtml.includes('content-limit:65536'));

const fallbackPresentation = createToolHumanGuidancePresentation({
  translate: () => { throw new Error('translator unavailable'); },
  renderMarkdown: () => { throw new Error('markdown unavailable'); },
});
let fallbackHtml = '';
let fallbackThrew = false;
try {
  fallbackHtml = fallbackPresentation.renderGuidanceHtml({
    status: 'awaiting_human',
    guidanceId: 'fallback',
    guidanceQuestion: '<plain>',
  }, { iconHtml: '' });
} catch { fallbackThrew = true; }
check('dependency_failures_degrade_without_exposing_html',
  fallbackThrew === false
  && fallbackHtml.includes('&lt;plain&gt;')
  && !fallbackHtml.includes('<plain>')
  && fallbackHtml.includes('project.hgPanelTitle'));

const received = { choice: [], free: [] };
globalThis.pwned = undefined;
installActionRegistry((name) => {
  if (name === 'submitHumanGuidanceChoice') {
    return (...args) => received.choice.push(args);
  }
  if (name === 'submitHumanGuidanceFreeText') {
    return (...args) => received.free.push(args);
  }
  return undefined;
});
document.body.innerHTML = choiceHtml;
document.querySelector('button.hg-option-card').click();
check('real_registry_click_delivers_exact_dataset_values',
  received.choice[0]?.[0] instanceof HTMLElement
  && received.choice[0][0].dataset.gid === hostileId
  && received.choice[0][0].dataset.label === hostileLabel
  && globalThis.pwned === undefined);

document.body.innerHTML = freeHtml;
const textarea = document.querySelector('textarea.hg-textarea');
const plainEnter = new window.KeyboardEvent(
  'keydown',
  { key: 'Enter', bubbles: true, cancelable: true },
);
textarea.dispatchEvent(plainEnter);
const ctrlEnter = new window.KeyboardEvent(
  'keydown',
  { key: 'Enter', ctrlKey: true, bubbles: true, cancelable: true },
);
textarea.dispatchEvent(ctrlEnter);
check('real_registry_keydown_obeys_modifier_and_dataset_contract',
  received.free.length === 1
  && received.free[0][0] === textarea
  && ctrlEnter.defaultPrevented === true
  && plainEnter.defaultPrevented === false);
document.querySelector('button.hg-submit-btn').click();
check('real_registry_submit_click_uses_same_static_contract',
  received.free.length === 2
  && received.free[1][0] instanceof HTMLButtonElement
  && received.free[1][0].dataset.gid === hostileId
  && globalThis.pwned === undefined);

check('only_bounded_values_cross_markdown_port',
  markdownInputs.every((value) => value.length <= 32769));

report();
"""


def test_tool_human_guidance_owner_and_actions():
    run_harness(
        target_js=str(OWNER_JS),
        extra_targets=[str(ACTION_REGISTRY_JS)],
        body_js=_OWNER_HARNESS,
        expect_pass=24,
        label='tool Human Guidance presentation owner',
    )
