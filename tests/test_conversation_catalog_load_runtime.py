"""Behavior contract for authoritative typed conversation catalog loading."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/conversation/application/conversation-catalog-loader.ts'
OWNER_BUNDLE = native_module_path('.native/conversation-catalog-loader.js', OWNER)


def _run_node_harness(harness: str) -> str:
    result = subprocess.run(
        ['node', '-e', harness, OWNER_BUNDLE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (result.stdout or '') + (result.stderr or '')
    assert result.returncode == 0, output
    return output


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_authoritative_rows_apply_without_waiting_for_cache_storage():
    harness = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));

const events = [];
const appliedIds = new Set();
const warnings = [];
let requestCount = 0;
let cacheWriteCount = 0;

const loader = createConversationCatalogLoader({
  requestCatalog: async () => {
    requestCount += 1;
    const item = {
      id: `conv-${requestCount}`,
      messageCount: 1,
      updatedAt: requestCount,
    };
    return {
      status: 200,
      ok: true,
      headers: {
        get: (name) => name === 'ETag' ? `"etag-${requestCount}"`
          : name === 'X-Total-Count' ? '100' : null,
      },
      json: async () => ({ items: [item] }),
    };
  },
  applyAuthoritativeRows: (rows) => {
    events.push('apply');
    for (const row of rows) appliedIds.add(row.id);
    return rows.map((row) => row.id);
  },
  hasEveryAppliedRow: (ids) => [...ids].every((id) => appliedIds.has(id)),
  writeCache: () => {
    cacheWriteCount += 1;
    events.push('cache');
    return new Promise(() => {});
  },
  wait: async () => {},
  warn: (message) => warnings.push(message),
});

async function settlesWithin(promise, milliseconds) {
  return Promise.race([
    promise.then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), milliseconds)),
  ]);
}

(async () => {
  const firstLoad = loader.load();
  const duplicateLoad = loader.load();
  const settled = [await settlesWithin(firstLoad, 100)];
  for (let index = 1; index < 3; index += 1) {
    settled.push(await settlesWithin(loader.load(), 100));
  }
  const checks = {
    concurrent_loads_share_one_request: firstLoad === duplicateLoad,
    loads_settle_while_cache_is_stuck: settled.every(Boolean),
    authoritative_rows_are_applied: appliedIds.size === 3,
    every_server_page_is_applied:
      events.filter((event) => event === 'apply').length === 3,
    apply_precedes_cache_write: events[0] === 'apply',
    cache_writes_remain_bounded: cacheWriteCount === 1,
    resource_budget_is_explicit:
      CONVERSATION_CATALOG_CACHE_WRITE_BUDGET.maximumInFlight === 1
        && CONVERSATION_CATALOG_CACHE_WRITE_BUDGET.maximumPending === 1,
    server_load_reports_success: loader.serverLoadOk() === true,
    no_warning_for_blocked_best_effort_cache: warnings.length === 0,
  };
  loader.destroy();
  for (const [name, passed] of Object.entries(checks)) {
    console.log(`${passed ? 'PASS' : 'FAIL'} ${name}`);
  }
  if (Object.values(checks).some((passed) => !passed)) process.exitCode = 1;
})().catch((error) => { console.error(error); process.exit(1); });
"""
    output = _run_node_harness(harness)
    assert output.count('PASS') == 9, output


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_not_modified_response_refetches_when_applied_snapshot_is_missing():
    harness = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));

const presentIds = new Set();
const requestHeaders = [];
const warnings = [];
let requestCount = 0;
const serverRows = [
  { id: 'server-conversation', messageCount: 1 },
  { id: 'empty-storage-shell', messageCount: 0 },
];

const loader = createConversationCatalogLoader({
  requestCatalog: async (request) => {
    requestCount += 1;
    requestHeaders.push({ ...request.headers });
    if (requestCount === 2 || requestCount === 3) {
      return {
        status: 304,
        ok: false,
        headers: { get: () => null },
        json: async () => ({}),
      };
    }
    return {
      status: 200,
      ok: true,
      headers: {
        get: (name) => name === 'ETag' ? '"catalog-v1"'
          : name === 'X-Total-Count' ? '2' : null,
      },
      json: async () => ({ items: serverRows }),
    };
  },
  applyAuthoritativeRows: (rows) => {
    const retained = rows.filter((row) => row.messageCount > 0);
    for (const row of retained) presentIds.add(row.id);
    return retained.map((row) => row.id);
  },
  hasEveryAppliedRow: (ids) => [...ids].every((id) => presentIds.has(id)),
  writeCache: async () => {},
  wait: async () => {},
  warn: (message) => warnings.push(message),
});

(async () => {
  await loader.load();
  await loader.load();
  const acceptedNotModifiedWithCompleteSnapshot = requestCount === 2;
  presentIds.clear();
  await loader.load();
  const checks = {
    conditional_request_was_attempted:
      requestHeaders[1]?.['If-None-Match'] === '"catalog-v1"',
    complete_snapshot_accepts_not_modified:
      acceptedNotModifiedWithCompleteSnapshot,
    missing_snapshot_forces_unconditional_refetch:
      requestCount === 4
        && requestHeaders[2]?.['If-None-Match'] === '"catalog-v1"'
        && !('If-None-Match' in requestHeaders[3]),
    authoritative_catalog_is_restored:
      presentIds.has('server-conversation'),
    recovered_load_reports_success: loader.serverLoadOk() === true,
    total_count_survives_revalidation: loader.serverTotalCount() === 2,
    recovery_is_silent: warnings.length === 0,
  };
  loader.destroy();
  for (const [name, passed] of Object.entries(checks)) {
    console.log(`${passed ? 'PASS' : 'FAIL'} ${name}`);
  }
  if (Object.values(checks).some((passed) => !passed)) process.exitCode = 1;
})().catch((error) => { console.error(error); process.exit(1); });
"""
    output = _run_node_harness(harness)
    assert output.count('PASS') == 7, output


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_failed_five_hundred_row_apply_does_not_commit_response_validator():
    harness = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));

const rows = Array.from({ length: 500 }, (_, index) => ({
  id: `catalog-${index}`,
  messageCount: 1,
}));
const presentIds = new Set();
const requestHeaders = [];
const warnings = [];
let applyCount = 0;

const loader = createConversationCatalogLoader({
  requestCatalog: async (request) => {
    requestHeaders.push({ ...request.headers });
    if (requestHeaders.length === 3) {
      return {
        status: 304,
        ok: false,
        headers: { get: () => null },
        json: async () => ({}),
      };
    }
    return {
      status: 200,
      ok: true,
      headers: {
        get: (name) => name === 'ETag' ? '"catalog-500"'
          : name === 'X-Total-Count' ? '500' : null,
      },
      json: async () => ({ items: rows }),
    };
  },
  applyAuthoritativeRows: (receivedRows) => {
    applyCount += 1;
    if (applyCount === 1) throw new RangeError('synthetic apply failure');
    for (const row of receivedRows) presentIds.add(row.id);
    return receivedRows.map((row) => row.id);
  },
  hasEveryAppliedRow: (ids) => [...ids].every((id) => presentIds.has(id)),
  writeCache: async () => {},
  wait: async () => {},
  warn: (message) => warnings.push(message),
});

(async () => {
  await loader.load();
  const failedLoadReportedFailure = loader.serverLoadOk() === false;
  await loader.load();
  await loader.load();
  const checks = {
    failed_apply_reports_failure: failedLoadReportedFailure,
    failed_apply_does_not_earn_etag:
      !('If-None-Match' in requestHeaders[1]),
    retry_needs_one_request_not_304_plus_full_page: requestHeaders.length === 3,
    successful_apply_commits_etag:
      requestHeaders[2]?.['If-None-Match'] === '"catalog-500"',
    all_rows_are_applied: presentIds.size === 500,
    accepted_304_reports_success: loader.serverLoadOk() === true,
    total_count_commits_with_successful_snapshot:
      loader.serverTotalCount() === 500,
    exact_apply_failure_is_observable:
      warnings.length === 1 && warnings[0] === 'synthetic apply failure',
  };
  loader.destroy();
  for (const [name, passed] of Object.entries(checks)) {
    console.log(`${passed ? 'PASS' : 'FAIL'} ${name}`);
  }
  if (Object.values(checks).some((passed) => !passed)) process.exitCode = 1;
})().catch((error) => { console.error(error); process.exit(1); });
"""
    output = _run_node_harness(harness)
    assert output.count('PASS') == 8, output
