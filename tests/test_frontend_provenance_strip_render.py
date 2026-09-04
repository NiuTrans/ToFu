"""Public behavior contract for the typed Turn-provenance presenter."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/conversation/presentation/turn-provenance.ts'
OWNER_JS = Path(native_module_path('.native/turn-provenance-contract.js', OWNER))

_HARNESS = r"""
eval(process.env.OWNER_SOURCE);

const checks = [];
function check(name, condition) {
  checks.push((condition ? 'PASS ' : 'FAIL ') + name);
}

const catalog = {
  'memPrefetch.prefetched': 'Prefetched {n} memory',
  'memPrefetch.prefetchedN': 'Prefetched {n} memories',
  'memPrefetch.candidatesN': '{n} candidates',
  'memPrefetch.tagN': '{n} memory',
  'memPrefetch.tagNs': '{n} memories',
  'prefs.appliedN': '{n} My Context items were provided this turn',
  'prefs.tagN': '{n} context item',
  'prefs.tagNs': '{n} context items',
  'relatedConvs.tagN': '{n} related chat',
  'relatedConvs.tagNs': '{n} related chats',
  'mcpDelta.tag': 'MCP tools',
  'mcpDelta.added': '1 MCP tool connected',
  'mcpDelta.addedN': '{n} MCP tools connected',
  'mcpDelta.removed': '1 MCP tool disconnected',
  'mcpDelta.removedN': '{n} MCP tools disconnected',
  'mcpDelta.addedTag': 'connected',
  'mcpDelta.removedTag': 'dropped',
  'mcpDelta.sub': 'schemas injected at the tail; call via execute_tools',
  'pathChange.tag': 'Project path',
  'pathChange.headline': 'Project path changed',
  'prefs.learnedTagN': 'remembered {n} context item',
  'prefs.learnedTagNs': 'remembered {n} context items',
};
function translate(key, params) {
  let value = catalog[key] || key;
  if (!params) return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name]) : token
  ));
}
function iconHtml(name, size) {
  return `<svg data-icon="${name}" data-size="${size}"></svg>`;
}
function decodeHtmlAttribute(value) {
  return value
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
}
function actionForClass(html, className) {
  const start = html.indexOf(`class="${className}"`);
  if (start < 0) return '';
  const match = html.slice(start).match(/data-tofu-action="([^"]+)"/);
  return match ? decodeHtmlAttribute(match[1]) : '';
}

const presentation = createTurnProvenancePresentation({ translate, iconHtml });
check('immutable_public_port', Object.isFrozen(presentation));
check('dom_free_public_surface',
  typeof presentation.inlineMarkdown === 'function'
  && typeof presentation.renderMcpLoginHintHtml === 'function'
  && typeof presentation.renderTurnProvenanceHtml === 'function'
  && typeof presentation.renderPreferenceLearnedHtml === 'function');

const longDescription = 'Centralized tool-arg repair: **param-KEY alias** '
  + 'layer (`file_path`->path) that fixes calls without rejection.';
let html = presentation.renderTurnProvenanceHtml({
  memoryPrefetch: {
    phase: 'done', selected: 1, candidates: 4, totalMs: 12,
    memories: [{
      name: 'tool-input-repair', scope: 'project',
      description: longDescription,
    }],
  },
});
check('memory_description_is_complete', html.includes('without rejection.'));
check('memory_inline_bold', html.includes('<strong>param-KEY alias</strong>'));
check('memory_inline_code', html.includes('<code>file_path</code>'));
check('memory_metadata_is_visible',
  html.includes('tool-input-repair') && html.includes('project')
  && html.includes('4 candidates') && html.includes('12ms'));

html = presentation.renderTurnProvenanceHtml({
  preferencesApplied: {
    chars: 200,
    items: [
      '**Language:** Output in Chinese.',
      'Use `listings` with *tofucode* for traces.',
    ],
  },
});
check('preference_list_is_rendered', html.includes('mp-mem-list pa-list'));
check('preference_inline_markdown',
  html.includes('<strong>Language:</strong>')
  && html.includes('<code>listings</code>')
  && html.includes('<em>tofucode</em>'));
check('generated_translator_params_are_used',
  html.includes('2 My Context items were provided this turn'));

const hostileInline = presentation.inlineMarkdown(
  '<script>alert(1)</script> **safe** `*literal*`',
);
check('inline_html_is_escaped_before_emphasis',
  !hostileInline.includes('<script>')
  && hostileInline.includes('&lt;script&gt;')
  && hostileInline.includes('<strong>safe</strong>'));
check('code_span_does_not_parse_emphasis',
  hostileInline.includes('<code>*literal*</code>'));

const longTitle = '阅读报告模式强制刷新后仍应显示完整的相关对话标题';
const hostileConversationId = "conv');globalThis.provenanceInjected=true;//";
html = presentation.renderTurnProvenanceHtml({
  relatedConversations: {
    count: 1,
    items: [{
      id: hostileConversationId,
      title: longTitle,
      summary: '<img src=x onerror=alert(1)> complete summary',
    }],
  },
});
check('related_conversation_content_is_complete_and_escaped',
  html.includes(longTitle) && html.includes('complete summary')
  && !html.includes('<img src=x'));
const openAction = actionForClass(html, 'rc-conv-link');
let openedConversationId = '';
globalThis.provenanceInjected = false;
new Function('event', 'loadConversation', openAction)(
  { stopPropagation() {} },
  (conversationId) => { openedConversationId = conversationId; },
);
check('related_conversation_action_round_trips_hostile_id',
  openedConversationId === hostileConversationId
  && globalThis.provenanceInjected === false);

const informational = [
  { kind: 'added', summary: 'Reply in **Chinese**', pending: false },
  { kind: 'reinforced', summary: 'Use `pytest`', pending: false },
];
html = presentation.renderTurnProvenanceHtml({ preferencesLearned: informational });
check('informational_preferences_fold_into_strip',
  html.includes('tp-seg-prefs-learned')
  && html.includes('<strong>Chinese</strong>'));
check('informational_preferences_do_not_duplicate_box',
  presentation.renderPreferenceLearnedHtml(informational) === '');

const hostilePreferenceId = "pref');globalThis.provenanceInjected=true;//";
const pendingHtml = presentation.renderPreferenceLearnedHtml([
  ...informational,
  {
    kind: 'pending', pending: true, id: hostilePreferenceId,
    summary: '<script>pending</script>',
  },
]);
check('pending_preference_is_the_only_prominent_row',
  pendingHtml.includes('pl-pending')
  && !pendingHtml.includes('pl-reinforced')
  && !pendingHtml.includes('<script>'));
const confirmAction = actionForClass(pendingHtml, 'pl-btn pl-confirm');
let resolvedPreference = null;
globalThis.provenanceInjected = false;
new Function('resolvePreference', confirmAction)(
  (_button, preferenceId, accepted) => {
    resolvedPreference = [preferenceId, accepted];
  },
);
check('preference_action_round_trips_hostile_id',
  JSON.stringify(resolvedPreference) === JSON.stringify([hostilePreferenceId, true])
  && globalThis.provenanceInjected === false);

const awaitingHtml = presentation.renderMcpLoginHintHtml({
  phase: 'awaiting_approval', username: '<owner>',
});
check('awaiting_login_stays_prominent',
  awaitingHtml.includes('mp-login-hint')
  && awaitingHtml.includes('&lt;owner&gt;')
  && presentation.renderTurnProvenanceHtml({
    mcpLoginHint: { phase: 'awaiting_approval' },
  }) === '');
const deniedHtml = presentation.renderTurnProvenanceHtml({
  mcpLoginHint: {
    phase: 'denied', snippet: '```json\n{"reason":"<denied>"}\n```',
  },
});
check('resolved_login_folds_with_failure_state',
  deniedHtml.includes('tp-has-failed')
  && deniedHtml.includes('mp-snippet')
  && deniedHtml.includes('&lt;denied&gt;')
  && presentation.renderMcpLoginHintHtml({ phase: 'denied' }) === '');

check('strip_state_precedence',
  presentation.renderTurnProvenanceHtml({
    memoryPrefetch: { phase: 'started' },
  }).includes('tp-running')
  && presentation.renderTurnProvenanceHtml({
    memoryPrefetch: { phase: 'done', selected: 0 },
  }).includes('tp-done')
  && presentation.renderTurnProvenanceHtml({
    memoryPrefetch: { phase: 'failed' },
    preferencesApplied: { items: [] },
  }).includes('tp-has-failed'));

html = presentation.renderTurnProvenanceHtml({
  mcpToolsDelta: {
    added: ['mcp__docs__write', 'mcp__docs__delete', '<img src=x>'],
    removed: ['mcp__docs__read'],
    total: 3,
  },
});
check('mcp_delta_segment_renders_counts_and_escaped_names',
  html.includes('tp-seg-mcp')
  && html.includes('3 MCP tools connected')
  && html.includes('1 MCP tool disconnected')
  && html.includes('mcp__docs__write')
  && html.includes('>connected</span>')
  && html.includes('>dropped</span>')
  && !html.includes('<img src=x'));

html = presentation.renderTurnProvenanceHtml({
  projectPathChange: { from: '/tmp/<alpha>', to: '/tmp/beta' },
});
check('path_change_segment_renders_from_to_escaped',
  html.includes('tp-seg-path')
  && html.includes('&lt;alpha&gt;')
  && html.includes('/tmp/beta'));

check('delta_segments_fail_closed_on_invalid_shapes',
  presentation.renderTurnProvenanceHtml({ mcpToolsDelta: { added: 'no' } }) === ''
  && presentation.renderTurnProvenanceHtml({ projectPathChange: {} }) === ''
  && !presentation.renderTurnProvenanceHtml({
    mcpToolsDelta: { added: [], removed: [] },
  }).includes('tp-seg-mcp'));
const frozenInput = Object.freeze({
  memoryPrefetch: Object.freeze({
    phase: 'done', selected: 1,
    memories: Object.freeze([Object.freeze({ name: 'stable' })]),
  }),
});
const before = JSON.stringify(frozenInput);
presentation.renderTurnProvenanceHtml(frozenInput);
check('projection_is_not_mutated', JSON.stringify(frozenInput) === before);
check('invalid_and_legacy_shapes_fail_closed',
  presentation.renderTurnProvenanceHtml(null) === ''
  && presentation.renderTurnProvenanceHtml({
    _memoryPrefetch: { phase: 'started' },
  }) === ''
  && presentation.renderPreferenceLearnedHtml([null, 'pending']) === '');

const hostileCopyPresentation = createTurnProvenancePresentation({
  translate: () => '<img src=x onerror=alert(1)>',
  iconHtml,
});
const hostileCopyHtml = hostileCopyPresentation.renderMcpLoginHintHtml({
  phase: 'awaiting_approval',
});
check('translated_copy_is_escaped_but_trusted_icons_remain_markup',
  !hostileCopyHtml.includes('<img src=x')
  && hostileCopyHtml.includes('&lt;img src=x')
  && hostileCopyHtml.includes('<svg'));

console.log(checks.join('\n'));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_turn_provenance_presentation_public_contract() -> None:
    source = OWNER.read_text(encoding='utf-8')
    assert 'runtimeScope' not in source
    assert 'globalThis' not in source
    assert 'window.' not in source
    assert 'document.' not in source

    process = subprocess.run(
        [shutil.which('node'), '-e', _HARNESS],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            'OWNER_SOURCE': OWNER_JS.read_text(encoding='utf-8'),
        },
    )
    assert process.returncode == 0, process.stderr
    failures = [
        line for line in process.stdout.splitlines() if line.startswith('FAIL ')
    ]
    assert not failures, process.stdout
    passes = [
        line for line in process.stdout.splitlines() if line.startswith('PASS ')
    ]
    assert len(passes) == 26, process.stdout
