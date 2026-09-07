"""Compaction viewer loads bounded projections before archived transcripts."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._jsdom import JS_DIR, run_harness


pytestmark = pytest.mark.unit
VIEWER = str(Path(JS_DIR) / "compaction-viewer.js")


_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

let listCalls = 0;
let summaryCalls = 0;
let rawCalls = 0;
const hugeContent = 'A'.repeat(18_000) + 'TAIL';
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

const { window, document, check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2]],
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
  check('Viewer opens on the summary tab',
    drawer.classList.contains('is-open')
    && drawer.querySelector('[data-tab="summary"]').classList.contains('is-active'));
  check('Initial open fetches list and summary projection only',
    listCalls === 1 && summaryCalls === 1 && rawCalls === 0);
  check('Summary projection renders without archived messages',
    drawer.querySelector('.cd-summary code').textContent === 'bounded continuation receipt');
  check('Structured receipt renders strategy and retained working files',
    drawer.querySelector('.cd-receipt').textContent.includes('selective_summary')
    && drawer.querySelector('.cd-receipt').textContent.includes('lib/app.py'));
  check('Structured receipt exposes summary usage and cache economics',
    drawer.querySelector('.cd-receipt').textContent.includes('1.8k + 600')
    && drawer.querySelector('.cd-receipt').textContent.includes('12.0k'));
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

  drawer.querySelector('[data-tab="messages"]').click();
  await flushEvents();
  check('Messages tab lazily fetches the raw projection once',
    rawCalls === 1 && !!drawer.querySelector('.compaction-msg-list'));
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
  drawer.querySelector('[data-tab="messages"]').click();
  await flushEvents();
  check('Repeated tab switches reuse the selected raw payload', rawCalls === 1);
  report();
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""


def test_summary_first_lazy_raw_and_large_content_reveal():
    run_harness(
        target_js=VIEWER,
        body_js=_HARNESS,
        expect_pass=13,
        label="compaction viewer projection flow",
    )
