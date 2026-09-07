"""Behavior contract for the owner-scoped conversation metadata cache."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend/src/core/conversation-metadata-cache.ts"
LAZY_SOURCE = ROOT / "frontend/src/core/conversation-metadata-cache-lazy.ts"


def test_metadata_cache_is_owner_scoped_and_resource_bounded():
    bundle = native_module_path("conversation-metadata-cache.js", SOURCE)
    harness = r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

let currentOwner = 1;
let available = true;
const metadata = new Map();
const sidebars = new Map();
const observations = {
  metadataMaximum: 0,
  sidebarMaximum: 0,
  sidebarInputRows: 0,
  metadataWrites: 0,
};
const key = (ownerId, id) => `${ownerId}:${id}`;
const publicRows = (values, ownerId) => [...values.values()]
  .filter((row) => row.ownerId === ownerId)
  .map(({ownerId: _ownerId, ...row}) => row);
const evict = (values, maximum) => {
  while (values.size > maximum) {
    const oldest = [...values.entries()]
      .sort((left, right) => left[1].cachedAt - right[1].cachedAt)[0];
    values.delete(oldest[0]);
  }
};
const storage = {
  isAvailable: () => available,
  listMetadata: async (ownerId) => publicRows(metadata, ownerId),
  listSidebar: async (ownerId) => publicRows(sidebars, ownerId),
  replaceSidebar: async (ownerId, rows, maximum) => {
    observations.sidebarMaximum = maximum;
    observations.sidebarInputRows = rows.length;
    for (const [rowKey, row] of sidebars) {
      if (row.ownerId === ownerId) sidebars.delete(rowKey);
    }
    for (const row of rows) sidebars.set(key(ownerId, row.id), row);
    evict(sidebars, maximum);
    return rows.length;
  },
  putMetadata: async (row, maximum) => {
    observations.metadataMaximum = maximum;
    observations.metadataWrites += 1;
    metadata.set(key(row.ownerId, row.id), row);
    evict(metadata, maximum);
  },
  remove: async (ownerId, id) => {
    metadata.delete(key(ownerId, id));
    sidebars.delete(key(ownerId, id));
  },
  clearOwner: async (ownerId) => {
    for (const [rowKey, row] of metadata) {
      if (row.ownerId === ownerId) metadata.delete(rowKey);
    }
    for (const [rowKey, row] of sidebars) {
      if (row.ownerId === ownerId) sidebars.delete(rowKey);
    }
  },
  countMetadata: async (ownerId) => publicRows(metadata, ownerId).length,
  close: () => { available = false; },
};
let clock = 1000;
const cache = createConversationMetadataCache({
  storage,
  resolveOwnerId: async () => currentOwner,
  now: () => ++clock,
});

(async () => {
  await cache.put({
    id: 'shared', title: 'Owner one', preset: 'opus', _serverTurnCount: 7,
  });
  const ownerOne = await cache.getAllMeta();
  currentOwner = 2;
  const ownerTwoBefore = await cache.getAllMeta();
  await cache.put({id: 'shared', title: 'Owner two', model: 'sonnet'});
  const ownerTwo = await cache.getAllMeta();

  currentOwner = 1;
  await cache.put({
    id: 'summary', model: 'opus',
    autopilotSummaries: ['x'.repeat(140 * 1024)],
  });
  await cache.put({
    id: 'too-large', model: 'opus', projectPaths: ['x'.repeat(140 * 1024)],
  });
  const ownerOneAfterLimits = await cache.getAllMeta();
  const summary = ownerOneAfterLimits.find((row) => row.id === 'summary');

  const sidebarInput = Array.from({length: 1005}, (_, index) => ({
    id: `conv-${index}`,
    title: `Conversation ${index}`,
    updatedAt: index,
    settings: {model: 'opus'},
  }));
  sidebarInput.push({
    id: 'oversized-sidebar', updatedAt: 9999,
    settings: {payload: 'x'.repeat(40 * 1024)},
  });
  const sidebarWritten = await cache.putSidebarList(sidebarInput);

  await cache.clear();
  currentOwner = 2;
  const ownerTwoAfterOwnerOneClear = await cache.getAllMeta();
  const stats = await cache.stats();
  const writesBeforeMissingOwner = observations.metadataWrites;
  currentOwner = null;
  await cache.put({id: 'missing-owner'});
  const missingOwnerDidNotWrite = observations.metadataWrites === writesBeforeMissingOwner;

  const unavailableStorage = createIndexedDbConversationMetadataCacheStorage(undefined);
  const unavailableCache = createConversationMetadataCache({
    storage: unavailableStorage,
    resolveOwnerId: () => 1,
  });
  await unavailableCache.put({id: 'ignored'});
  const unavailable = {
    available: unavailableCache.isAvailable(),
    rows: await unavailableCache.getAllMeta(),
  };

  cache.close();
  console.log(JSON.stringify({
    ownerOneModel: ownerOne[0].settings.model,
    ownerOneCount: ownerOne.length,
    ownerTwoBeforeCount: ownerTwoBefore.length,
    ownerTwoModel: ownerTwo[0].settings.model,
    summaryTrimmed: summary && !('autopilotSummaries' in summary.settings),
    oversizedMetadataSkipped: !ownerOneAfterLimits.some((row) => row.id === 'too-large'),
    sidebarWritten,
    sidebarInputRows: observations.sidebarInputRows,
    sidebarMaximum: observations.sidebarMaximum,
    metadataMaximum: observations.metadataMaximum,
    ownerTwoSurvivesClear: ownerTwoAfterOwnerOneClear.length === 1,
    stats,
    missingOwnerDidNotWrite,
    unavailable,
    closed: !cache.isAvailable(),
    presetFallback: extractConversationCacheSettings({preset: 'fallback'}).model,
    imageRoute: extractConversationCacheSettings({
      imageGenModel: 'image-model', imageGenProviderId: 'image-provider',
      imageGenCount: 3, imageGenAspect: '16:9', imageGenResolution: '2K',
    }),
    budget: CONVERSATION_METADATA_CACHE_RESOURCE_BUDGET,
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        ["node", "-e", harness, bundle],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == {
        "ownerOneModel": "opus",
        "ownerOneCount": 1,
        "ownerTwoBeforeCount": 0,
        "ownerTwoModel": "sonnet",
        "summaryTrimmed": True,
        "oversizedMetadataSkipped": True,
        "sidebarWritten": 1000,
        "sidebarInputRows": 1000,
        "sidebarMaximum": 1000,
        "metadataMaximum": 200,
        "ownerTwoSurvivesClear": True,
        "stats": {"count": 1, "messageCount": 0, "available": True},
        "missingOwnerDidNotWrite": True,
        "unavailable": {"available": False, "rows": []},
        "closed": True,
        "presetFallback": "fallback",
        "imageRoute": {
            "imageGenModel": "image-model",
            "imageGenProviderId": "image-provider",
            "imageGenCount": 3,
            "imageGenAspect": "16:9",
            "imageGenResolution": "2K",
        },
        "budget": {
            "maximumMetadataRows": 200,
            "maximumSidebarRows": 1000,
            "maximumMetadataRowBytes": 128 * 1024,
            "maximumSidebarRowBytes": 32 * 1024,
            "maximumEstimatedBytes": 58_982_400,
        },
    }


def test_lazy_cache_coalesces_first_demand_and_closes_racing_load():
    bundle = native_module_path("conversation-metadata-cache-lazy.js", LAZY_SOURCE)
    harness = r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

let loadCount = 0;
let closeCount = 0;
let putCount = 0;
const child = {
  isAvailable: () => true,
  getAllMeta: async () => [{id: 'cached'}],
  getSidebarList: async () => [],
  putSidebarList: async (rows) => rows.length,
  put: async () => { putCount += 1; },
  remove: async () => undefined,
  clear: async () => undefined,
  stats: async () => ({count: 1, messageCount: 0, available: true}),
  close: () => { closeCount += 1; },
};
const proxy = createLazyConversationMetadataCache({
  isCapabilityAvailable: () => true,
  load: async () => {
    loadCount += 1;
    await Promise.resolve();
    return child;
  },
});

(async () => {
  const [rows] = await Promise.all([
    proxy.getAllMeta(),
    proxy.put({id: 'one'}),
    proxy.stats(),
  ]);
  const availableBeforeClose = proxy.isAvailable();
  proxy.close();
  proxy.close();
  const rowsAfterClose = await proxy.getAllMeta();
  await proxy.put({id: 'ignored'});

  let releaseRacingLoad;
  let racingCloseCount = 0;
  const racingChild = {
    ...child,
    close: () => { racingCloseCount += 1; },
  };
  const racingProxy = createLazyConversationMetadataCache({
    isCapabilityAvailable: () => true,
    load: () => new Promise((resolve) => {
      releaseRacingLoad = () => resolve(racingChild);
    }),
  });
  const racingRead = racingProxy.getAllMeta();
  await Promise.resolve();
  racingProxy.close();
  releaseRacingLoad();
  const racingRows = await racingRead;
  console.log(JSON.stringify({
    loadCount,
    putCount,
    rows,
    availableBeforeClose,
    availableAfterClose: proxy.isAvailable(),
    rowsAfterClose,
    closeCount,
    racingCloseCount,
    racingRows,
    racingAvailable: racingProxy.isAvailable(),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        ["node", "-e", harness, bundle],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "loadCount": 1,
        "putCount": 1,
        "rows": [{"id": "cached"}],
        "availableBeforeClose": True,
        "availableAfterClose": False,
        "rowsAfterClose": [],
        "closeCount": 1,
        "racingCloseCount": 1,
        "racingRows": [],
        "racingAvailable": False,
    }
