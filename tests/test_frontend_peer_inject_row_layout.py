"""Peer-injection attribution through the typed presentation boundary.

The row identifies source conversations by title, deduplicates a single sender,
caps multi-sender attribution, and exposes one inert ``data-conv-jump`` intent.
Presentation is tested through the real TypeScript owner; the retained runtime
test below covers only its still-live navigation lifecycle responsibility.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OWNER_JS = Path(native_module_path(
    '.native/tool-injection-peer-layout.js',
    ROOT
    / 'frontend/src/conversation/presentation/'
    / 'tool-injection-presentation.ts',
))
RETAINED_TOOL_ROUNDS_JS = Path(JS_DIR) / 'ui/tool_rounds.js'


_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2]],
});
const messages = {
  'peer.injectRowLabel': 'Received',
  'peer.injectRowOne': 'peer message',
  'peer.injectRowMany': 'peer messages',
  'peer.injectRowBadge': 'injected → context',
  'peer.jumpToConv': 'Open conversation',
  'peerCard.noPayload': 'No message available.',
  'toolInjection.itemsLimit': 'Showing first {shown} of {total} injected items.',
  'toolInjection.contentLimit': 'Content truncated to the first {n} characters.',
};
function translate(key, params) {
  let value = messages[key] || key;
  if (!params || typeof params !== 'object') return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name]) : token
  ));
}
const titles = {
  mrnaj25i: '修复显示层 Bug',
  sibaaaa1: '队列注入',
  sibbbbb2: 'inbox 重建',
  sibccccc3: '前缀缓存',
  sibddddd4: '桌面发布',
};
function create(resolveConversationTitle) {
  return createToolInjectionPresentation({
    translate,
    renderMarkdown: (source) => `<p>${source}</p>`,
    iconHtml: () => '',
    resolveConversationTitle,
  });
}
function occurrences(value, fragment) {
  return String(value).split(fragment).length - 1;
}
function headerOf(html) {
  return html.slice(html.indexOf('<summary'), html.indexOf('</summary>'));
}
function bodyOf(html) {
  return html.slice(html.indexOf('</summary>'));
}
function hasRawIdLabel(html) {
  return /(>|\[)(sib[a-z0-9]+|mrnaj25i)(<|,|\s|\])/.test(
    html.replace(/title="[^"]*"/g, ''),
  );
}

const presentation = create((conversationId) => titles[conversationId] || '');
const html = presentation.renderInjectionHtml({
  _peerInject: true,
  roundNum: 9000002,
  peerCount: 1,
  peerPreviews: [{
    fromConv: 'mrnaj25i',
    text: 'Heads up: fixing the bug',
  }],
});
check('title_bubble_present', html.includes('修复显示层 Bug'));
check('title_bubble_class', html.includes('sw-peer-from-bubble'));
check('raw_id_not_a_visible_label', !html.includes('>mrnaj25i<'));
check('bubble_is_jump_button',
  html.includes('<button') && html.includes('data-conv-jump="mrnaj25i"'));
check('tooltip_has_full_title_raw_id_and_hint',
  /title="[^"]*修复显示层 Bug[^"]*mrnaj25i[^"]*Open conversation"/.test(html));
check('model_view_affordance_stays_absent',
  !html.includes('data-tc-preview') && !html.includes('tc-preview-btn'));
check('dead_chevron_span_stays_absent', !html.includes('sw-inbox-row-chev'));
check('per_card_raw_toggle_stays_absent', !html.includes('sw-card-raw'));
check('single_sender_is_not_repeated_in_body',
  !bodyOf(html).includes('sw-peer-from-bubble'));

const multi = presentation.renderInjectionHtml({
  _peerInject: true,
  roundNum: 9000003,
  peerCount: 5,
  peerPreviews: [
    { fromConv: 'sibaaaa1', text: 'a' },
    { fromConv: 'sibbbbb2', text: 'b' },
    { fromConv: 'sibccccc3', text: 'c' },
    { fromConv: 'sibddddd4', text: 'd' },
    { fromConv: 'mrnaj25i', text: 'e' },
  ],
});
const multiHeader = headerOf(multi);
check('multi_header_has_title_bubbles',
  occurrences(multiHeader, 'sw-peer-from-bubble') === 3);
check('multi_header_has_one_group',
  occurrences(multiHeader, 'sw-peer-from-group') === 1);
check('multi_header_has_visible_overflow',
  multiHeader.includes('sw-peer-from-more') && multiHeader.includes('+2'));
check('multi_header_has_no_raw_id_label', !hasRawIdLabel(multiHeader));
check('multi_header_has_no_legacy_id_list',
  !multi.includes('sw-inbox-row-ids'));
check('multi_header_uses_resolved_titles',
  multiHeader.includes('队列注入')
  && multiHeader.includes('inbox 重建')
  && multiHeader.includes('前缀缓存'));
check('multi_sender_body_keeps_per_card_attribution',
  occurrences(bodyOf(multi), 'sw-peer-from-bubble') === 5);

const neutered = create(() => 'Untitled chat');
const neuteredHtml = neutered.renderInjectionHtml({
  _peerInject: true,
  peerPreviews: [{ fromConv: 'mrnaj25i', text: 'message' }],
});
check('neutered_title_port_removes_specific_title',
  !neuteredHtml.includes('修复显示层 Bug')
  && neuteredHtml.includes('Untitled chat'));
const neuteredMulti = neutered.renderInjectionHtml({
  _peerInject: true,
  peerPreviews: [
    { fromConv: 'sibaaaa1', text: 'a' },
    { fromConv: 'sibbbbb2', text: 'b' },
  ],
});
check('neutered_multi_sender_still_uses_bubbles',
  occurrences(headerOf(neuteredMulti), 'sw-peer-from-bubble') === 2
  && !hasRawIdLabel(headerOf(neuteredMulti)));

report();
"""


_JUMP_HARNESS = r"""
const navigation = [];
const toasts = [];
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2]],
  globals: {
    conversations: [],
    activeConvId: null,
    convFullIdById: (conversationId) => {
      navigation.push(['resolve', conversationId]);
      return conversationId === 'short-id' ? 'full-conversation-id' : '';
    },
    loadConversation: (conversationId) => {
      navigation.push(['load', conversationId]);
    },
    showToast: (...args) => { toasts.push(args); },
  },
});

function clickBubble(conversationId) {
  const bubble = document.createElement('button');
  bubble.className = 'sw-peer-from-bubble';
  bubble.setAttribute('data-conv-jump', conversationId);
  const child = document.createElement('span');
  bubble.appendChild(child);
  document.body.appendChild(bubble);
  child.dispatchEvent(new window.MouseEvent('click', {
    bubbles: true,
    cancelable: true,
  }));
}

clickBubble('short-id');
check('jump_intent_resolves_and_loads_the_full_conversation_id',
  JSON.stringify(navigation) === JSON.stringify([
    ['resolve', 'short-id'],
    ['load', 'full-conversation-id'],
  ])
  && toasts.length === 0);

clickBubble('missing-id');
check('unresolved_jump_is_visible_and_never_loads_a_partial_id',
  navigation.some((entry) => entry[0] === 'resolve' && entry[1] === 'missing-id')
  && !navigation.some((entry) => entry[0] === 'load' && entry[1] === 'missing-id')
  && toasts.length === 1);

report();
"""


def test_peer_injection_layout_uses_typed_title_port():
    run_harness(
        target_js=str(OWNER_JS),
        body_js=_HARNESS,
        expect_pass=18,
        label='peer injection typed layout',
    )


def test_retained_runtime_owns_only_the_jump_lifecycle():
    run_harness(
        target_js=str(RETAINED_TOOL_ROUNDS_JS),
        body_js=_JUMP_HARNESS,
        expect_pass=2,
        label='peer injection jump lifecycle',
    )
