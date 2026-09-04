"""Exact DOM, transport, and lifecycle contract for Human Guidance actions."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
ACTIONS_JS = Path(native_module_path(
    '.native/human-guidance-actions-contract.js',
    ROOT / 'frontend/src/conversation/ui/human-guidance-actions.ts',
))
PRESENTER_JS = Path(native_module_path(
    '.native/human-guidance-actions-presenter.js',
    ROOT
    / 'frontend/src/conversation/presentation/'
    / 'tool-human-guidance-presentation.ts',
))
REGISTRY_JS = Path(native_module_path(
    '.native/human-guidance-actions-registry.js',
    ROOT / 'frontend/src/action-registry.ts',
))


_HARNESS = r"""
const messages = {
  'project.hgExpired': 'expired-badge-local',
  'project.hgChoiceNotePlaceholder': 'note-placeholder-local',
  'project.hgExpiredToast': 'expired-toast-local',
  'project.hgNetworkError': 'network-local',
  'project.hgPanelTitle': 'panel-local',
  'project.hgSubmit': 'submit-local',
  'project.hgSubmitFailed': 'submit-failed-local',
  'project.hgTextareaPlaceholder': 'placeholder-local',
  'project.hgTranslateFailed': 'translate-failed-local',
  'project.hgTranslating': 'translating-local',
  'project.hgWaitingReply': 'reply-local',
  'toolHumanGuidance.defaultQuestion': 'default-question-local',
  'toolHumanGuidance.optionFallback': 'option-local:{index}',
  'toolHumanGuidance.responseLimit': 'response-limit:{n}',
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
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2], process.argv[4], process.argv[5]],
});

let active = { conversationId: 'conv-a', autoTranslate: false };
let translateBehavior = async (source) => source;
let submitBehavior = async () => ({ ok: true });
let lateBehavior = async () => ({ ok: true });
const translations = [];
const requests = [];
const lateRequests = [];
const marks = [];
const renders = [];
const listRenders = [];
const toasts = [];
const logs = [];
const scheduled = [];
const actions = createHumanGuidanceActions({
  translate,
  activeConversation: () => active,
  translateResponse: async (source) => {
    translations.push(source);
    return translateBehavior(source);
  },
  submitResponse: async (request) => {
    requests.push(request);
    return submitBehavior(request);
  },
  submitLateAnswer: async (request) => {
    lateRequests.push(request);
    return lateBehavior(request);
  },
  markSubmitted: (...args) => marks.push(args),
  requestExpiredRender: (conversationId) => renders.push(conversationId),
  renderConversationList: () => listRenders.push(true),
  showToast: (...args) => toasts.push(args),
  log: (...args) => logs.push(args),
  schedule: (callback, delayMs) => {
    scheduled.push([callback, delayMs]);
    return scheduled.length;
  },
});
const presenter = createToolHumanGuidancePresentation({
  translate,
  renderMarkdown: (source) => '<md>' + escaped(source) + '</md>',
});
installActionRegistry((name) => {
  if (name === 'submitHumanGuidanceChoice') {
    return (element) => actions.submitChoice(element);
  }
  if (name === 'submitHumanGuidanceFreeText') {
    return (element) => actions.submitFreeText(element);
  }
  return undefined;
});
async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}
function mount(round) {
  document.body.innerHTML = presenter.renderGuidanceHtml(
    round,
    { iconHtml: '<trusted-icon>', toolDisplayLabel: 'Guidance' },
  );
  return document.querySelector('.hg-card');
}
function choiceRound(guidanceId, label) {
  return {
    status: 'awaiting_human', toolName: 'ask_human',
    guidanceId: guidanceId || 'choice-id',
    guidanceQuestion: 'Pick one', guidanceType: 'choice',
    guidanceOptions: [{ label: label || 'Choice A' }, { label: 'Choice B' }],
  };
}
function freeRound(guidanceId) {
  return {
    status: 'awaiting_human', toolName: 'ask_human',
    guidanceId: guidanceId || 'free-id',
    guidanceQuestion: 'Explain', guidanceType: 'free_text',
  };
}

(async () => {
  const limits = HUMAN_GUIDANCE_ACTION_LIMITS;
  check('limits_and_port_are_frozen_and_narrow',
    Object.isFrozen(limits) && limits.identifierUnits === 512
    && limits.choiceLabelUnits === 1024 && limits.responseUnits === 32768
    && limits.optionItems === 16 && Object.isFrozen(actions)
    && Object.keys(actions).sort().join(',') ===
      'destroy,submitChoice,submitFreeText');

  const hostileId = "hg'\"] button{display:none}";
  const hostileLabel = "choice'\");globalThis.pwned=true;//";
  let card = mount(choiceRound(hostileId, hostileLabel));
  const hostileButton = card.querySelector('button.hg-option-card');
  globalThis.pwned = undefined;
  hostileButton.click();
  await flush();
  check('hostile_choice_uses_card_scoped_static_selectors',
    requests.at(-1).guidanceId === hostileId
    && requests.at(-1).responseText === hostileLabel
    && globalThis.pwned === undefined);
  check('successful_choice_projects_optimistic_and_local_state',
    hostileButton.disabled && hostileButton.classList.contains('hg-selected')
    && card.classList.contains('hg-submitting')
    && JSON.stringify(marks.at(-1)) === JSON.stringify([
      'conv-a', hostileId, hostileLabel,
    ]) && listRenders.length === 1);

  let deferredResolve;
  submitBehavior = () => new Promise((resolve) => { deferredResolve = resolve; });
  card = mount(choiceRound('dedup-id'));
  const dedupButton = card.querySelector('button.hg-option-card');
  const beforeDedup = requests.length;
  dedupButton.click();
  dedupButton.click();
  await flush();
  check('duplicate_choice_is_single_flight',
    requests.length === beforeDedup + 1
    && card.classList.contains('hg-submitting'));
  deferredResolve({ ok: true });
  await flush();

  // ── Choice notes (one independent bounded draft per option) ──
  submitBehavior = async () => ({ ok: true });
  card = mount(choiceRound('note-id', 'Choice A'));
  const noteInputs = [...card.querySelectorAll('textarea.hg-option-note-input')];
  check('each_choice_renders_its_own_bounded_note_input',
    noteInputs.length === 2
    && noteInputs.every((input, index) => (
      input.dataset.gid === 'note-id'
      && input.dataset.optionIndex === String(index)
      && input.maxLength === 4096
      && input.placeholder === 'note-placeholder-local'
    )));
  noteInputs[0].value = '  用第一个方案  ';
  noteInputs[1].value = '  只属于第二个  ';
  card.querySelector('button.hg-option-card').click();
  await flush();
  check('choice_submit_sends_only_selected_option_note',
    requests.at(-1).responseText === 'Choice A\nuser_note: 用第一个方案'
    && !requests.at(-1).responseText.includes('只属于第二个')
    && JSON.stringify(marks.at(-1)) === JSON.stringify([
      'conv-a', 'note-id', 'Choice A\nuser_note: 用第一个方案',
    ]));

  active = { conversationId: 'conv-cn', autoTranslate: true };
  translateBehavior = async () => 'use this plan';
  card = mount(choiceRound('note-cn', 'Choice A'));
  card.querySelector('textarea.hg-option-note-input').value = '备注内容';
  card.querySelector('button.hg-option-card').click();
  await flush();
  check('chinese_selected_note_is_translated_original_preserved',
    translations.at(-1) === '备注内容'
    && requests.at(-1).responseText === 'Choice A\nuser_note: use this plan'
    && JSON.stringify(marks.at(-1)) === JSON.stringify([
      'conv-cn', 'note-cn', 'Choice A\nuser_note: 备注内容',
    ]));

  translateBehavior = async () => { throw new Error('translator down'); };
  card = mount(choiceRound('note-fail'));
  card.querySelector('textarea.hg-option-note-input').value = '备注原文';
  card.querySelector('button.hg-option-card').click();
  await flush();
  check('selected_note_translation_failure_falls_back_to_original',
    requests.at(-1).responseText === 'Choice A\nuser_note: 备注原文'
    && toasts.at(-1)[0] === 'translate-failed-local');
  translateBehavior = async (source) => source;
  active = { conversationId: 'conv-a', autoTranslate: false };

  card = mount(choiceRound('note-long'));
  card.querySelector('textarea.hg-option-note-input').value = 'N'.repeat(5000);
  card.querySelector('button.hg-option-card').click();
  await flush();
  check('oversized_selected_note_is_sliced_to_note_limit',
    requests.at(-1).responseText
      === `Choice A\nuser_note: ${'N'.repeat(4096)}`);

  card = mount(choiceRound('note-keyboard'));
  const keyboardNote = card.querySelectorAll('textarea.hg-option-note-input')[1];
  keyboardNote.value = 'second details';
  keyboardNote.dispatchEvent(new window.KeyboardEvent(
    'keydown', { key: 'Enter', ctrlKey: true, bubbles: true, cancelable: true },
  ));
  await flush();
  check('note_keyboard_submit_selects_its_own_option',
    requests.at(-1).responseText === 'Choice B\nuser_note: second details'
    && card.querySelectorAll('button.hg-option-card')[1]
      .classList.contains('hg-selected'));

  const beforeTamperedNote = requests.length;
  card = mount(choiceRound('note-tamper'));
  card.querySelectorAll('textarea.hg-option-note-input')[1]
    .dataset.optionIndex = '0';
  card.querySelectorAll('button.hg-option-card')[1].click();
  await flush();
  check('mismatched_option_note_identity_defaults_deny',
    requests.length === beforeTamperedNote);

  card = mount(freeRound('no-note-free'));
  check('free_text_card_has_no_choice_note_input',
    !card.querySelector('textarea.hg-option-note-input')
    && Boolean(card.querySelector('textarea.hg-textarea')));

  card = mount({ ...choiceRound('note-expired'), _turnSettled: true });
  check('expired_choice_card_renders_no_note_input',
    !card.querySelector('textarea.hg-option-note-input')
    && !card.querySelector('button.hg-option-card'));

  submitBehavior = async () => ({ ok: true });
  active = { conversationId: 'conv-cn', autoTranslate: true };
  translateBehavior = async () => 'English answer';
  card = mount(freeRound('free-cn'));
  const textarea = card.querySelector('textarea');
  const submitButton = card.querySelector('button.hg-submit-btn');
  const originalButtonHtml = submitButton.innerHTML;
  textarea.value = '中文回答';
  submitButton.click();
  await flush();
  check('free_text_translation_submits_translated_payload',
    translations.at(-1) === '中文回答'
    && JSON.stringify(requests.at(-1)) === JSON.stringify({
      conversationId: 'conv-cn', guidanceId: 'free-cn',
      responseText: 'English answer',
    }));
  check('free_text_preserves_original_for_optimistic_projection',
    JSON.stringify(marks.at(-1)) === JSON.stringify([
      'conv-cn', 'free-cn', '中文回答',
    ]) && !submitButton.disabled && submitButton.innerHTML === originalButtonHtml
    && textarea.maxLength === 32768);

  const beforeEmpty = requests.length;
  card = mount(freeRound('empty'));
  const emptyTextarea = card.querySelector('textarea');
  card.querySelector('button.hg-submit-btn').click();
  await flush();
  check('empty_response_shakes_without_transport',
    requests.length === beforeEmpty
    && emptyTextarea.classList.contains('hg-shake')
    && scheduled.at(-1)[1] === 500);
  scheduled.at(-1)[0]();
  check('empty_response_shake_has_explicit_cleanup',
    !emptyTextarea.classList.contains('hg-shake'));

  const beforeOversized = requests.length;
  card = mount(freeRound('oversized'));
  card.querySelector('textarea').value = 'X'.repeat(32769);
  card.querySelector('button.hg-submit-btn').click();
  await flush();
  check('oversized_response_fails_before_translation_or_transport',
    requests.length === beforeOversized
    && toasts.at(-1)[0] === 'response-limit:32768'
    && toasts.at(-1)[1] === 'error');

  translateBehavior = async () => { throw new Error('translator down'); };
  card = mount(freeRound('fallback'));
  card.querySelector('textarea').value = '中文原文';
  card.querySelector('button.hg-submit-btn').click();
  await flush();
  check('translation_failure_falls_back_to_bounded_original',
    requests.at(-1).guidanceId === 'fallback'
    && requests.at(-1).responseText === '中文原文'
    && toasts.at(-1)[0] === 'translate-failed-local'
    && toasts.at(-1)[1] === 'warning');

  active = { conversationId: 'conv-a', autoTranslate: false };
  submitBehavior = async () => ({ error: 'server refused' });
  card = mount(choiceRound('refused'));
  const refusedButtons = [...card.querySelectorAll('button.hg-option-card')];
  refusedButtons[0].click();
  await flush();
  check('typed_transport_error_restores_exact_choice_state',
    !card.classList.contains('hg-submitting')
    && refusedButtons.every((button) => !button.disabled)
    && refusedButtons.every((button) => !button.classList.contains('hg-selected'))
    && toasts.at(-1)[0] === 'submit-failed-local');

  submitBehavior = async () => {
    const error = new Error('gone');
    error.status = 404;
    throw error;
  };
  card = mount(choiceRound('expired'));
  card.querySelector('button.hg-option-card').click();
  await flush();
  check('not_found_is_expired_and_requests_authoritative_render',
    renders.at(-1) === 'conv-a' && toasts.at(-1)[0] === 'expired-toast-local'
    && toasts.at(-1)[1] === 'warning'
    && !card.classList.contains('hg-submitting'));

  const renderCount = renders.length;
  submitBehavior = async () => { throw new TypeError('network down'); };
  card = mount(choiceRound('network'));
  card.querySelector('button.hg-option-card').click();
  await flush();
  check('network_failure_is_distinct_from_expiry',
    renders.length === renderCount && toasts.at(-1)[0] === 'network-local'
    && toasts.at(-1)[1] === 'error');

  const beforeNoActive = requests.length;
  active = null;
  card = mount(choiceRound('no-active'));
  card.querySelector('button.hg-option-card').click();
  await flush();
  check('missing_active_conversation_defaults_deny',
    requests.length === beforeNoActive
    && !card.classList.contains('hg-submitting'));

  active = { conversationId: 'conv-a', autoTranslate: false };
  const beforeMismatch = requests.length;
  card = mount(choiceRound('match-a'));
  card.dataset.gid = 'match-b';
  card.querySelector('button.hg-option-card').click();
  await flush();
  check('mismatched_dom_identity_defaults_deny',
    requests.length === beforeMismatch);

  const beforeOptionOverflow = requests.length;
  card = mount(choiceRound('bounded-options'));
  const overflowSource = card.querySelector('.hg-option-group');
  for (let index = 0; index < 16; index += 1) {
    card.querySelector('.hg-options-grid').prepend(overflowSource.cloneNode(true));
  }
  overflowSource.querySelector('button.hg-option-card').click();
  await flush();
  check('bounded_dom_option_scan_defaults_deny_beyond_limit',
    requests.length === beforeOptionOverflow
    && !card.classList.contains('hg-submitting'));

  card = mount(choiceRound('L'.repeat(513)));
  check('presenter_and_actions_share_identifier_limit',
    !card.querySelector('[data-tofu-action]')
    && card.querySelector('.hg-unavailable'));

  active = { conversationId: 'conv-a', autoTranslate: false };
  card = mount({
    ...choiceRound('late-id', 'Choice A'),
    _turnSettled: true, _hgAnswerGuidance: true, _turnId: 'turn-1',
  });
  check('late_answerable_choice_keeps_options_and_notes_interactive',
    !card.classList.contains('hg-expired')
    && card.dataset.hgLateAnswer === '1' && card.dataset.turnId === 'turn-1'
    && card.querySelectorAll('button.hg-option-card').length === 2
    && card.querySelectorAll('textarea.hg-option-note-input').length === 2
    && card.querySelector('.hg-badge').textContent.trim() === 'reply-local');
  card.querySelectorAll('textarea.hg-option-note-input')[1].value = 'late note';
  card.querySelectorAll('button.hg-option-card')[1].click();
  await flush();
  check('late_choice_answer_routes_to_attempt_command_with_turn_identity',
    (requests.length === 0 || requests.at(-1).guidanceId !== 'late-id')
    && lateRequests.length === 1
    && JSON.stringify(lateRequests.at(-1)) === JSON.stringify({
      conversationId: 'conv-a', turnId: 'turn-1',
      guidanceId: 'late-id', responseText: 'Choice B\nuser_note: late note',
    })
    && JSON.stringify(marks.at(-1)) === JSON.stringify([
      'conv-a', 'late-id', 'Choice B\nuser_note: late note',
    ]));

  card = mount({ ...freeRound('dead-id'), _turnSettled: true });
  check('expired_card_without_offer_stays_static',
    card.classList.contains('hg-expired')
    && !card.querySelector('textarea')
    && !card.dataset.hgLateAnswer
    && card.querySelector('.hg-badge').textContent.trim()
      === 'expired-badge-local');

  actions.destroy();
  const beforeDestroy = requests.length;
  card = mount(choiceRound('destroyed'));
  card.querySelector('button.hg-option-card').click();
  await flush();
  check('destroy_refuses_new_work_and_releases_single_flight_state',
    requests.length === beforeDestroy
    && !card.classList.contains('hg-submitting'));
  check('diagnostics_are_injected_and_non_authoritative',
    logs.some((entry) => entry[0] === 'success')
    && logs.some((entry) => entry[0] === 'error'));
  report();
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""


def test_human_guidance_actions_contract():
    run_harness(
        target_js=str(ACTIONS_JS),
        extra_targets=[str(PRESENTER_JS), str(REGISTRY_JS)],
        body_js=_HARNESS,
        expect_pass=31,
        label='Human Guidance actions',
    )
