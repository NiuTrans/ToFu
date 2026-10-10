"""Compaction viewer renders only the post-compaction messages projection."""

from __future__ import annotations

from pathlib import Path

import json
import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import ROOT, native_module_path

SNAPSHOT = native_module_path(
    '.native/compaction-snapshot.js',
    ROOT / 'frontend/src/features/compaction-snapshot.ts',
)


pytestmark = pytest.mark.unit
VIEWER = str(Path(JS_DIR) / "compaction-viewer.js")

_FIXTURES = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

let listCalls = 0;
let summaryCalls = 0;
let rawCalls = 0;
let postCalls = 0;
const hugeContent = 'A'.repeat(18_000) + 'TAIL';
let copied = null;
let downloadQuery = null;
const archiveListItem = {
  id: 'archive-1',
  convId: 'conversation-1',
  trigger: 'working_set',
  roundNum: 7,
  tokensBefore: 20_000,
  tokensAfter: 8_000,
  tokenCountKind: 'estimated',
  msgsBefore: 12,
  msgsAfter: 5,
  resultStatus: 'completed',
  resultStrategy: 'selective_summary',
  payloadSize: hugeContent.length,
  createdAt: 1_724_587_200_000,
};
const summaryArchive = {
  ...archiveListItem,
  summary: 'bounded continuation receipt',
  taskModel: 'gpt-task',
  model: 'gpt-list',
  receipt: {
    schemaVersion: 'tofu.compaction-receipt/v1',
    status: 'completed',
    strategy: 'selective_summary',
    implementation: 'model_summary',
    mode: 'turns_and_intra_turn',
    continuation: { format: 'context_compact_tool' },
    summary: {
      generated: true,
      accepted: true,
      chars: 1234,
      durationMs: 1250,
      projectedUsageTokens: 3000,
      usage: { inputTokens: 1800, outputTokens: 600 },
    },
    retention: {
      summarizedMessages: 7,
      preservedTurns: 2,
      foldedToolRounds: 3,
      objectiveAnchored: true,
      retainedUserMessages: 1,
      recentFiles: ['lib/app.py'],
      turnDiffIncluded: true,
    },
    economics: {
      droppedTokens: 12000,
      cacheRewriteTokens: 2000,
      summaryCostTokens: 2400,
      paybackRounds: 0.8,
    },
  },
};
const postMessages = [
  { role: 'system', content: 'post-system-prompt' },
  { role: 'user', content: '### Objective\nkept working state' },
  { role: 'tool', content: hugeContent },
  { role: 'user', content: '<retained_user_messages>fake</retained_user_messages>' },
  { role: 'assistant', content: null, tool_calls: [{ id: 'compact-1', function: {name: 'context_compact'} }] },
  { role: 'tool', tool_call_id: 'compact-1', content: 'bounded continuation receipt' },
];
"""

_HARNESS = _FIXTURES + r"""
const { window, document, check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[4], process.argv[2]],
  globals: {
    runtimeScope: {},
    _applyI18n: () => {},
    Api: {
      compactions: {
        list: async () => {
          listCalls += 1;
          return { compactions: [archiveListItem] };
        },
        getSummary: async () => {
          summaryCalls += 1;
          return { archive: summaryArchive };
        },
        getPost: async () => {
          postCalls += 1;
          return { archive: summaryArchive, view: 'post', postMessages };
        },
        get: async () => {
          rawCalls += 1;
          return {
            archive: summaryArchive,
            messages: [{ role: 'tool', content: hugeContent }],
          };
        },
      },
    },
  },
});

async function flushEvents() {
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
}

(async () => {
  await runtimeScope.openCompactionViewer('conversation-1');
  const drawer = document.getElementById('compactionViewerDrawer');
  check('Viewer opens on the messages tab',
    drawer.classList.contains('is-open')
    && drawer.querySelector('[data-tab="messages"]').classList.contains('is-active'));
  check('Initial open fetches list, summary, and post projection only',
    listCalls === 1 && summaryCalls === 1 && postCalls === 1 && rawCalls === 0);
  check('Post-compaction messages render by default',
    drawer.querySelector('.compaction-msg-list').textContent.includes('post-system-prompt')
    && drawer.querySelector('.compaction-msg-list').textContent.includes('kept working state'));
  check('No pre/post view toggle is rendered',
    !drawer.querySelector('[data-msg-view]'));
  check('Estimated counters and task model are explicit',
    drawer.querySelector('.compaction-drawer-meta').textContent.includes('≈20.0k')
    && drawer.querySelector('.compaction-drawer-meta').textContent.includes('gpt-task'));
  check('Millisecond archive timestamp is not multiplied again',
    drawer.querySelector('.compaction-drawer-meta').textContent.includes('2024'));
  const cachedHistory = runtimeScope.getCompactionHistory('conversation-1');
  check('History cache preserves archive result semantics',
    cachedHistory[0].tokenCountKind === 'estimated'
    && cachedHistory[0].payloadSize === hugeContent.length
    && cachedHistory[0].resultStatus === 'completed'
    && cachedHistory[0].resultStrategy === 'selective_summary');

  check('Summary identity comes from adjacent call, not user XML',
    drawer.querySelectorAll('[data-compaction-kind="summary"]').length === 1
    && drawer.querySelectorAll('.cd-msg')[3].dataset.compactionKind === 'unknown');
  drawer.querySelector('[data-jump-summary]').click();
  check('Jump focuses summary without changing source order',
    document.activeElement.dataset.compactionKind === 'summary'
    && drawer.querySelectorAll('.cd-msg')[5].textContent.includes('bounded continuation receipt'));
  const previewCode = drawer.querySelector('.cd-large-content code');
  check('Large tool result enters the DOM as a bounded preview',
    previewCode.textContent.length < hugeContent.length
    && previewCode.textContent.includes('full content not rendered'));
  const reveal = drawer.querySelector('[data-reveal-content]');
  check('Large result offers an explicit reveal action', !!reveal);
  reveal.click();
  check('Reveal replaces the preview with exact full content',
    drawer.querySelector('.cd-large-content code').textContent === hugeContent
    && !drawer.querySelector('[data-reveal-content]'));

  drawer.querySelector('[data-tab="summary"]').click();
  await flushEvents();
  check('Summary tab renders receipt, usage economics, and summary text',
    drawer.querySelector('.cd-receipt').textContent.includes('selective_summary')
    && drawer.querySelector('.cd-receipt').textContent.includes('lib/app.py')
    && drawer.querySelector('.cd-receipt').textContent.includes('1.8k + 600')
    && drawer.querySelector('.cd-receipt').textContent.includes('12.0k')
    && drawer.querySelector('.cd-summary code').textContent === 'bounded continuation receipt');

  drawer.querySelector('[data-tab="messages"]').click();
  await flushEvents();
  check('Repeated tab switches reuse the cached post payload only',
    rawCalls === 0 && postCalls === 1 && summaryCalls === 1
    && drawer.querySelector('.compaction-msg-list').textContent.includes('post-system-prompt'));
  report();
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""

_FALLBACK_HARNESS = _FIXTURES + r"""
const { window, document, check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[4], process.argv[2]],
  globals: {
    runtimeScope: {},
    _applyI18n: () => {},
    Api: {
      compactions: {
        list: async () => {
          listCalls += 1;
          return { compactions: [archiveListItem] };
        },
        getSummary: async () => {
          summaryCalls += 1;
          return { archive: summaryArchive };
        },
        getPost: async () => {
          postCalls += 1;
          return { archive: summaryArchive, view: 'post', postMessages: null };
        },
        get: async () => {
          rawCalls += 1;
          return {
            archive: summaryArchive,
            messages: [{ role: 'tool', content: hugeContent }],
          };
        },
      },
    },
  },
});

async function flushEvents() {
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
}

(async () => {
  await runtimeScope.openCompactionViewer('conversation-1');
  const drawer = document.getElementById('compactionViewerDrawer');
  Object.defineProperty(global, 'navigator', {value: window.navigator, configurable: true});
  Object.defineProperty(global.navigator, 'clipboard', { value: { writeText: async text => { copied = JSON.parse(text); } }, configurable: true });
  drawer.querySelector('[data-action="copy-json"]').click();
  await flushEvents();
  check('Unavailable export stays null with stage metadata',
    copied.postMessages === null && copied.postSnapshot.status === 'unavailable'
    && copied.postSnapshot.stage === 'compaction_result');
  check('Legacy archive still opens on the messages tab',
    drawer.classList.contains('is-open')
    && drawer.querySelector('[data-tab="messages"]').classList.contains('is-active'));
  check('Null post messages never fall back to the raw transcript',
    postCalls === 1 && rawCalls === 0);
  check('A note explains the missing post view',
    drawer.querySelector('.cd-empty').textContent.includes('postMissing'));
  check('No pre/post view toggle is rendered for legacy rows',
    !drawer.querySelector('[data-msg-view]'));
  report();
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""


def test_viewer_legacy_labels_match_backend_occurrence_contract():
    from lib.tasks_pkg.compaction._handoff import legacy_compaction_message_positions

    def batch(names, ids, results):
        return [{'role': 'assistant', 'tool_calls': [
            {'id': ident, 'function': {'name': name}} for name, ident in zip(names, ids)]},
            *[{'role': 'tool', 'tool_call_id': ident, 'content': 'state'} for ident in results]]

    compact = 'context_compact'
    complete = batch([compact, compact], ['same', 'same'], ['same', 'same'])
    cases = [complete, batch([compact, compact], ['a', 'b'], ['a']),
             batch([compact, 'read_files'], ['a', 'b'], ['a', 'b']),
             batch([compact], ['a'], ['a', 'orphan']),
             batch([compact], [''], ['']),
             [complete[-1]],
             [*complete, {'role': 'user', 'content': 'steer'},
              batch([compact], ['missing'], [])[0], complete[-1]]]
    expected = []
    for messages in cases:
        positions = legacy_compaction_message_positions(messages)
        expected.append(['summary' if i in positions and m['role'] == 'tool'
                         else 'unknown' for i, m in enumerate(messages)])
    body = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({ root: process.argv[3], targets: [process.argv[2]] });
const cases = CASES;
const expected = EXPECTED;
cases.forEach((messages, index) => {
  const before = JSON.stringify(messages);
  check('Backend/frontend legacy provenance ' + index,
    JSON.stringify(compactionMessageKinds(messages)) === JSON.stringify(expected[index]));
  check('Presentation leaves snapshot unchanged ' + index, JSON.stringify(messages) === before);
});
report();
'''.replace('CASES', json.dumps(cases)).replace('EXPECTED', json.dumps(expected))
    run_harness(target_js=SNAPSHOT, body_js=body, expect_pass=len(cases) * 2,
                label='compaction snapshot occurrence provenance')


def test_post_view_only_and_large_content_reveal():
    run_harness(
        target_js=VIEWER,
        extra_targets=[SNAPSHOT],
        body_js=_HARNESS,
        expect_pass=14,
        label="compaction viewer post-only flow",
    )


def test_legacy_archive_without_post_messages_shows_note():
    run_harness(
        target_js=VIEWER,
        extra_targets=[SNAPSHOT],
        body_js=_FALLBACK_HARNESS,
        expect_pass=5,
        label="compaction viewer legacy note",
    )
