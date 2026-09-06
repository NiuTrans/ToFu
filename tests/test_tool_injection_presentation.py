"""Exact owner contract for bounded synthetic injection-row presentation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OWNER_JS = Path(native_module_path(
    '.native/tool-injection-presentation-contract.js',
    ROOT
    / 'frontend/src/conversation/presentation/'
    / 'tool-injection-presentation.ts',
))


_OWNER_HARNESS = r"""
const messages = {
  'swarmCard.received': 'received-local',
  'swarmCard.updateOne': 'update-one-local',
  'swarmCard.updateMany': 'update-many-local',
  'swarmCard.remaining': 'remaining:{r}/{p}',
  'swarmCard.noPayload': 'no-swarm-local',
  'peerCard.noPayload': 'no-peer-local',
  'toolInjection.itemsLimit': 'items-limit:{shown}/{total}',
  'toolInjection.contentLimit': 'content-limit:{n}',
  'peer.injectRowLabel': 'peer-label-local',
  'peer.injectRowOne': 'peer-one-local',
  'peer.injectRowMany': 'peer-many-local',
  'peer.injectRowBadge': 'badge-local',
  'peer.jumpToConv': 'jump-local',
  'steer.injectRowLabel': 'steer-label-local',
  'steer.injectRowOne': 'steer-one-local',
  'steer.injectRowMany': 'steer-many-local',
  'steer.noPayload': 'no-steer-local',
  'stall.injectRowLabel': 'stall-label-local',
  'stall.reasonWithTool': 'stall-tool:{tool}',
  'stall.reasonGeneric': 'stall-generic-local',
  'stall.bound': 'stall-bound-local',
  'stall.promptLabel': 'stall-prompt-local',
  'bgCommand.injectRowLabel': 'bgcmd-label-local',
  'bgCommand.injectRowOne': 'bgcmd-one-local',
  'bgCommand.injectRowMany': 'bgcmd-many-local',
  'bgCommand.noPayload': 'no-bgcmd-local',
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
const titleInputs = [];
const titleById = {
  alpha: '<Alpha title>',
  beta: 'Beta title',
  gamma: 'Gamma title',
  delta: 'Delta title',
};
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2]],
});
const presentation = createToolInjectionPresentation({
  translate,
  renderMarkdown: (source) => {
    markdownInputs.push(source);
    return `<md>${escaped(source)}</md>`;
  },
  iconHtml: (name, size) => `<icon:${name}:${size}>`,
  resolveConversationTitle: (conversationId) => {
    titleInputs.push(conversationId);
    return titleById[conversationId] || '';
  },
});
function occurrences(value, fragment) {
  return String(value).split(fragment).length - 1;
}

const limits = TOOL_INJECTION_PRESENTATION_LIMITS;
check('limits_and_port_are_frozen_and_narrow',
  Object.isFrozen(limits)
  && limits.previewItems === 16
  && limits.agentIdentities === 4
  && limits.senderBubbles === 3
  && limits.identifierUnits === 512
  && limits.titleUnits === 512
  && limits.xmlInputUnits === 65536
  && limits.markdownUnits === 16384
  && limits.rawTextUnits === 16384
  && limits.stallPromptUnits === 32768
  && Object.isFrozen(presentation)
  && Object.keys(presentation).length === 1);
check('unrelated_values_fail_closed',
  presentation.renderInjectionHtml(null) === ''
  && presentation.renderInjectionHtml({ toolName: 'read_files' }) === '');

const inboxRound = Object.freeze({
  _inboxInject: true,
  roundNum: '7"><script>',
  inboxCount: 2,
  inboxAgentIds: Object.freeze(['<a>', 'b', 'c', 'd', 'e']),
  inboxPreviews: Object.freeze([
    Object.freeze({
      agentId: 'fallback-agent',
      text: '<swarm-update><agent-id>&lt;agent&gt;</agent-id>'
        + '<role>reviewer</role><status>completed</status>'
        + '<elapsed-seconds>3</elapsed-seconds><tokens>44</tokens>'
        + '<output-file>&lt;report.md&gt;</output-file>'
        + '<preview>**done**</preview>'
        + '<remaining running="1" pending="2"/></swarm-update>',
    }),
    Object.freeze({ agentId: '<raw-agent>', text: '<raw payload>' }),
  ]),
});
const inboxBefore = JSON.stringify(inboxRound);
const inboxHtml = presentation.renderInjectionHtml(inboxRound);
check('inbox_lane_parses_xml_and_projects_trusted_ports',
  inboxHtml.includes('class="sw-inbox-row"')
  && inboxHtml.includes('&lt;agent&gt;')
  && inboxHtml.includes('ptool-badge-ok')
  && inboxHtml.includes('remaining:1/2')
  && inboxHtml.includes('<md>**done**</md>')
  && inboxHtml.includes('<icon:file:11>'));
check('inbox_attributes_and_raw_payload_are_escaped',
  inboxHtml.includes('data-rn="7&quot;&gt;&lt;script&gt;"')
  && inboxHtml.includes('[&lt;a&gt;, b, c, d +1]')
  && inboxHtml.includes('&lt;raw-agent&gt;')
  && inboxHtml.includes('&lt;raw payload&gt;')
  && !inboxHtml.includes('<raw payload>'));
check('owner_never_mutates_inbox_input', JSON.stringify(inboxRound) === inboxBefore);

const failedInboxHtml = presentation.renderInjectionHtml({
  _inboxInject: true,
  inboxPreviews: [{
    agentId: 'fallback-id',
    text: '<task-notification><status>error</status>'
      + '<error>&lt;boom&gt;</error></task-notification>',
  }],
});
check('task_notification_uses_error_status_and_agent_fallback',
  failedInboxHtml.includes('fallback-id')
  && failedInboxHtml.includes('ptool-badge-err')
  && failedInboxHtml.includes('&lt;boom&gt;'));

const oversizedXmlHtml = presentation.renderInjectionHtml({
  _inboxInject: true,
  inboxPreviews: [{
    agentId: 'oversized',
    text: '<swarm-update><preview>' + 'X'.repeat(65536)
      + '</preview></swarm-update>TAIL',
  }],
});
check('oversized_xml_is_not_regex_parsed_and_raw_fallback_is_bounded',
  oversizedXmlHtml.includes('&lt;swarm-update&gt;')
  && oversizedXmlHtml.includes('content-limit:16384')
  && !oversizedXmlHtml.includes('TAIL'));

const manyInboxPreviews = Array.from({ length: 20 }, (_, index) => ({
  agentId: `agent-${index}`,
  text: `raw-${index}`,
}));
const manyInboxHtml = presentation.renderInjectionHtml({
  _inboxInject: true,
  inboxPreviews: manyInboxPreviews,
});
check('preview_item_count_is_bounded_and_visible',
  occurrences(manyInboxHtml, 'sw-card sw-card-rawonly') === 16
  && manyInboxHtml.includes('items-limit:16/20')
  && !manyInboxHtml.includes('raw-16'));

const peerHtml = presentation.renderInjectionHtml({
  _peerInject: true,
  roundNum: 9,
  peerPreviews: [
    { fromConv: 'alpha', text: '<first>' },
    { fromConv: 'beta', text: '**second**' },
  ],
});
check('peer_lane_resolves_titles_and_keeps_multi_sender_attribution',
  peerHtml.includes('sw-peer-row')
  && peerHtml.includes('&lt;Alpha title&gt;')
  && peerHtml.includes('Beta title')
  && occurrences(peerHtml, 'data-conv-jump="alpha"') === 2
  && occurrences(peerHtml, 'data-conv-jump="beta"') === 2
  && peerHtml.includes('<md>&lt;first&gt;</md>'));
const oneSenderHtml = presentation.renderInjectionHtml({
  _peerInject: true,
  peerPreviews: [
    { fromConv: 'alpha', text: 'one' },
    { fromConv: 'alpha', text: 'two' },
  ],
});
check('single_sender_is_not_repeated_inside_each_card',
  occurrences(oneSenderHtml, 'data-conv-jump="alpha"') === 1);

const senderCapHtml = presentation.renderInjectionHtml({
  _peerInject: true,
  peerPreviews: ['alpha', 'beta', 'gamma', 'delta'].map((fromConv) => ({
    fromConv, text: fromConv,
  })),
});
check('sender_bubbles_are_deduplicated_and_visibly_capped',
  senderCapHtml.includes('sw-peer-from-more">+1')
  && occurrences(senderCapHtml, 'class="sw-peer-from-group"') === 1);

const longPeerText = 'P'.repeat(16384) + 'PEER_TAIL';
const longPeerHtml = presentation.renderInjectionHtml({
  _peerInject: true,
  peerPreviews: [{ fromConv: "a' onclick='bad", text: longPeerText }],
});
check('peer_ids_and_markdown_inputs_are_bounded_and_escaped',
  longPeerHtml.includes('data-conv-jump="a&#39; onclick=&#39;bad"')
  && longPeerHtml.includes('content-limit:16384')
  && !longPeerHtml.includes('PEER_TAIL')
  && markdownInputs.at(-1).length === 16385);

const steerHtml = presentation.renderInjectionHtml({
  _userSteerInject: true,
  steerCount: 1,
  steerPreviews: [{ text: '<operator words>' }],
});
check('operator_steer_has_a_distinct_lane_and_bounded_markdown_port',
  steerHtml.includes('sw-steer-row')
  && steerHtml.includes('steer-label-local')
  && steerHtml.includes('steer-one-local')
  && steerHtml.includes('<md>&lt;operator words&gt;</md>')
  && !steerHtml.includes('sw-peer-row'));
const emptySteerHtml = presentation.renderInjectionHtml({
  _userSteerInject: true,
  steerPreviews: 'malformed',
});
check('malformed_preview_shapes_degrade_to_localized_empty_state',
  emptySteerHtml.includes('no-steer-local'));

const stallHtml = presentation.renderInjectionHtml({
  _stallNudge: true,
  roundNum: 11,
  stallTool: '<run_command>',
  stallPrompt: '<SYSTEM>' + 'S'.repeat(32768) + 'STALL_TAIL',
});
check('stall_lane_preserves_system_provenance_and_bound',
  stallHtml.includes('sw-stall-row')
  && stallHtml.includes('stall-tool:&lt;run_command&gt;')
  && stallHtml.includes('stall-bound-local')
  && stallHtml.includes('&lt;SYSTEM&gt;')
  && !stallHtml.includes('sw-steer-row'));
check('stall_prompt_is_bounded_with_a_visible_notice',
  stallHtml.includes('content-limit:32768')
  && !stallHtml.includes('STALL_TAIL'));

const bgcmdHtml = presentation.renderInjectionHtml({
  _bgCommandInject: true,
  roundNum: 5,
  bgCommandCount: 1,
  bgCommandPreviews: [{ commandId: 'bg_<abc>', text: '<raw result>' }],
});
check('bg_command_lane_renders_escaped_result_without_markdown_port',
  bgcmdHtml.includes('sw-bgcmd-row')
  && bgcmdHtml.includes('bgcmd-label-local')
  && bgcmdHtml.includes('bgcmd-one-local')
  && bgcmdHtml.includes('badge-local')
  && bgcmdHtml.includes('bg_&lt;abc&gt;')
  && bgcmdHtml.includes('&lt;raw result&gt;')
  && !bgcmdHtml.includes('<raw result>')
  && !bgcmdHtml.includes('sw-steer-row'));
const emptyBgcmdHtml = presentation.renderInjectionHtml({
  _bgCommandInject: true,
  bgCommandPreviews: 'malformed',
});
check('malformed_bg_command_previews_degrade_to_empty_state',
  emptyBgcmdHtml.includes('no-bgcmd-local'));

const priorityHtml = presentation.renderInjectionHtml({
  _inboxInject: true,
  _peerInject: true,
  _userSteerInject: true,
  _bgCommandInject: true,
  _stallNudge: true,
});
check('lane_priority_is_closed_and_deterministic',
  priorityHtml.includes('class="sw-inbox-row"')
  && !priorityHtml.includes('sw-peer-row')
  && !priorityHtml.includes('sw-steer-row')
  && !priorityHtml.includes('sw-bgcmd-row')
  && !priorityHtml.includes('sw-stall-row'));

const fallbackPresentation = createToolInjectionPresentation({
  translate,
  renderMarkdown: () => { throw new Error('markdown unavailable'); },
  iconHtml: () => { throw new Error('icon unavailable'); },
  resolveConversationTitle: () => { throw new Error('catalog unavailable'); },
});
let fallbackHtml = '';
let fallbackThrew = false;
try {
  fallbackHtml = fallbackPresentation.renderInjectionHtml({
    _peerInject: true,
    peerPreviews: [{ fromConv: '<cid>', text: '<plain>' }],
  });
} catch { fallbackThrew = true; }
check('dependency_failures_degrade_without_throwing_or_exposing_html',
  fallbackThrew === false
  && fallbackHtml.includes('&lt;cid&gt;')
  && fallbackHtml.includes('&lt;plain&gt;')
  && !fallbackHtml.includes('<plain>'));

check('only_bounded_values_cross_markdown_and_title_ports',
  markdownInputs.every((value) => value.length <= 16385)
  && titleInputs.every((value) => value.length <= 513));

report();
"""


def test_tool_injection_owner_contract():
    run_harness(
        target_js=str(OWNER_JS),
        body_js=_OWNER_HARNESS,
        expect_pass=21,
        label='tool injection presentation owner',
    )
