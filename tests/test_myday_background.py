"""Behavior and resource contracts for typed My Day background owners."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
CACHE_OWNER = ROOT / 'frontend/src/features/myday/report-cache.ts'
BACKGROUND_OWNER = ROOT / 'frontend/src/features/myday/background-controller.ts'
CACHE_BUNDLE = native_module_path('.native/myday-report-cache.js', CACHE_OWNER)
BACKGROUND_BUNDLE = native_module_path(
    '.native/myday-background-controller.js', BACKGROUND_OWNER,
)


def _run_node(harness: str, *bundles: str) -> dict[str, object]:
    result = subprocess.run(
        ['node', '-e', harness, *bundles], cwd=ROOT,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_report_cache_is_owner_scoped_byte_bounded_and_digest_driven():
    harness = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));

const stores = { reports: new Map(), months: new Map() };
const writeLimits = [];
const storage = {
  read: async (store, key) => stores[store].get(key)?.value ?? null,
  write: async (store, record, maximumEntries) => {
    writeLimits.push([store, maximumEntries]);
    stores[store].set(record.key, record);
    const ordered = [...stores[store].values()]
      .sort((left, right) => left.cachedAt - right.cachedAt);
    while (ordered.length > maximumEntries) {
      const oldest = ordered.shift();
      stores[store].delete(oldest.key);
    }
  },
};
let clock = 1000;
const cache = createMyDayPersistentCache({ storage, now: () => ++clock });
const budget = MYDAY_CACHE_RESOURCE_BUDGET;
const dateAt = (offset) => new Date(Date.UTC(2026, 0, 1 + offset))
  .toISOString().slice(0, 10);

(async () => {
  await cache.writeReport(7, '2026-01-01', { marker: 'owner-seven' });
  await cache.writeReport(8, '2026-01-01', { marker: 'owner-eight' });
  const ownerSeven = await cache.readReport(7, '2026-01-01');
  const ownerEight = await cache.readReport(8, '2026-01-01');
  const writesBeforeInvalid = writeLimits.length;
  await cache.writeReport(0, '2026-01-02', { marker: 'invalid-owner' });
  await cache.writeReport(7, 'bad-date', { marker: 'invalid-date' });
  await cache.writeReport(7, '2026-01-03', {
    text: 'x'.repeat(budget.maximumReportBytes + 1),
  });
  const invalidWritesSkipped = writeLimits.length === writesBeforeInvalid;

  for (let index = 0; index < budget.maximumReports + 7; index += 1) {
    await cache.writeReport(7, dateAt(index), { index });
  }
  for (let index = 0; index < budget.maximumMonths + 5; index += 1) {
    const year = 2020 + Math.floor(index / 12);
    const month = String((index % 12) + 1).padStart(2, '0');
    await cache.writeMonth(7, `${year}-${month}`, { index });
  }

  const digests = [];
  const repository = createMyDayReportRepository({
    cache,
    ownerId: () => 7,
    today: () => '2026-08-28',
    publishDigest: (digest) => digests.push(digest),
  });
  await repository.storeReport('2026-08-28', {
    streams: [
      { status: 'done' }, { status: 'blocked' }, { status: 'in_progress' },
    ],
    today_todos: [{ done: true }, { done: false }],
    stats: { totalConversations: 9 },
  });
  repository.publishReport('2026-08-27', { stats: { totalConversations: 99 } });

  console.log(JSON.stringify({
    budget,
    digests,
    invalidWritesSkipped,
    monthEntries: stores.months.size,
    ownerSeven: ownerSeven?.marker,
    ownerEight: ownerEight?.marker,
    reportEntries: stores.reports.size,
    reportLimitAlwaysApplied: writeLimits
      .filter(([store]) => store === 'reports')
      .every(([, limit]) => limit === budget.maximumReports),
    scopedKeys: [...stores.reports.keys()].every((key) => key.startsWith('owner:7:')),
  }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    output = _run_node(harness, CACHE_BUNDLE)
    assert output['budget'] == {
        'maximumReports': 96,
        'maximumMonths': 24,
        'maximumReportBytes': 512 * 1024,
        'maximumMonthBytes': 128 * 1024,
        'maximumEstimatedBytes': 51 * 1024 * 1024,
    }
    assert output['ownerSeven'] == 'owner-seven'
    assert output['ownerEight'] == 'owner-eight'
    assert output['invalidWritesSkipped'] is True
    assert output['reportEntries'] == 96
    assert output['monthEntries'] == 24
    assert output['reportLimitAlwaysApplied'] is True
    assert output['scopedKeys'] is True
    assert output['digests'] == [{
        'streams': {'total': 3, 'done': 1, 'blocked': 1},
        'todos': {'total': 2, 'done': 1},
        'convCount': 9,
    }]


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_background_controller_has_one_disposable_probe_and_bounded_reminders():
    harness = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));

let ownerId = 7;
let conversationCount = 4;
let reportOpen = false;
const scheduled = [];
const cancelled = [];
const published = [];
const stored = [];
const notices = [];
const apiCalls = { status: 0, count: 0 };
const storageValues = new Map();
const repository = {
  readReport: async () => ({ stats: { totalConversations: 2 } }),
  publishReport: (date, report) => published.push([date, report]),
  storeReport: async (date, report) => stored.push([date, report]),
  readMonth: async () => null,
  storeMonth: async () => {},
};
const controller = createMyDayBackgroundController({
  ownerId: () => ownerId,
  repository,
  readStatus: async () => {
    apiCalls.status += 1;
    return { status: 'done', report: { stats: { totalConversations: 5 } } };
  },
  readConversationCount: async () => {
    apiCalls.count += 1;
    return { count: conversationCount };
  },
  notify: (notice) => { notices.push([ownerId, notice]); return true; },
  translate: (key, values) => `${key}:${values?.n ?? ''}`,
  storage: {
    getItem: (key) => storageValues.get(key) ?? null,
    setItem: (key, value) => storageValues.set(key, value),
  },
  reportIsOpen: () => reportOpen,
  now: () => new Date('2026-08-28T15:00:00'),
  setTimeout: (callback, delay) => {
    const handle = { callback, delay };
    scheduled.push(handle);
    return handle;
  },
  clearTimeout: (handle) => cancelled.push(handle),
});

(async () => {
  controller.start();
  controller.start();
  scheduled.find((timer) => timer.delay === 0).callback();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await controller.refreshDigest();

  await controller.checkReminder();
  await controller.checkReminder();
  for (ownerId = 8; ownerId <= 30; ownerId += 1) {
    await controller.checkReminder();
  }
  const ledger = JSON.parse(storageValues.get(MYDAY_REMINDER_STORAGE_KEY));
  const noticesBeforeLowActivity = notices.length;
  ownerId = 31;
  conversationCount = 2;
  await controller.checkReminder();
  ownerId = 32;
  conversationCount = 4;
  reportOpen = true;
  await controller.checkReminder();
  controller.destroy();
  controller.destroy();
  const statusCallsBeforeDestroyedCallbacks = apiCalls.status;
  scheduled.forEach((timer) => timer.callback());
  await new Promise((resolve) => setTimeout(resolve, 0));

  console.log(JSON.stringify({
    apiCalls,
    cancelled: cancelled.length,
    digestStored: stored.length,
    ledgerLength: ledger.length,
    ledgerOwners: ledger.map((entry) => entry.ownerId),
    lowActivityAndOpenSkipped: notices.length === noticesBeforeLowActivity,
    notices: notices.length,
    published: published.length,
    scheduledDelays: scheduled.map((timer) => timer.delay),
    statusAfterDestroy: apiCalls.status === statusCallsBeforeDestroyedCallbacks,
  }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    output = _run_node(harness, BACKGROUND_BUNDLE)
    assert output['scheduledDelays'] == [0, 3 * 60 * 60 * 1000]
    assert output['cancelled'] == 2
    assert output['published'] == 1
    assert output['digestStored'] == 1
    assert output['apiCalls']['status'] == 1
    assert output['ledgerLength'] == 16
    assert output['ledgerOwners'] == list(range(15, 31))
    assert output['notices'] == 24
    assert output['lowActivityAndOpenSkipped'] is True
    assert output['statusAfterDestroy'] is True
