"""Bounded cost contract for revision-aware conversation catalog refreshes."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
OWNER = (
    ROOT / "frontend/src/conversation/application/"
    "conversation-catalog-revision-gate.ts"
)
OWNER_BUNDLE = native_module_path(
    ".native/conversation-catalog-revision-gate-contract.js",
    OWNER,
)


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_revision_gate_is_bounded_fail_soft_and_lifecycle_owned():
    harness = r"""
const fs = require('fs');
eval(fs.readFileSync(OWNER_PATH, 'utf8'));
const checks = [];
const check = (name, value) => checks.push(`${value ? 'PASS' : 'FAIL'} ${name}`);

const revisions = new Map([['conv-a', 4]]);
const timers = new Map();
const warnings = [];
let timerSequence = 0;
let refreshes = 0;
let visible = true;
let visibilityFailure = false;
let refreshFailure = null;
const gate = createConversationCatalogRevisionGate({
  readRevision: (conversationId) => revisions.get(conversationId) ?? null,
  refreshCatalog: () => {
    refreshes += 1;
    if (refreshFailure === 'sync') throw new Error('sync refresh failure');
    if (refreshFailure === 'async') return Promise.reject(
      new Error('async refresh failure'),
    );
    return Promise.resolve();
  },
  isVisible: () => {
    if (visibilityFailure) throw new Error('visibility failure');
    return visible;
  },
  setTimeout: (callback, delayMs) => {
    const id = ++timerSequence;
    timers.set(id, {callback, delayMs});
    return id;
  },
  clearTimeout: (id) => timers.delete(id),
  warn: (message) => warnings.push(message),
});
const runLatestTimer = () => {
  const entry = [...timers.entries()].at(-1);
  if (!entry) return false;
  timers.delete(entry[0]);
  entry[1].callback();
  return entry[1].delayMs;
};

(async () => {
  check('positive_reached_revision_skips_schedule', gate.reached('conv-a', 4));
  gate.schedule('conv-a', 4);
  check('reached_revision_allocates_no_timer', timers.size === 0);

  gate.schedule('conv-a', 7);
  gate.schedule('conv-a', 6);
  revisions.set('conv-a', 6);
  check('reschedule_keeps_one_timer', timers.size === 1);
  check('debounce_delay_is_explicit', runLatestTimer()
    === CONVERSATION_CATALOG_REFRESH_DELAY_MS);
  await Promise.resolve();
  check('highest_revision_requires_refresh', refreshes === 1);

  for (let index = 0; index <= CONVERSATION_CATALOG_REVISION_BUDGET; index += 1) {
    gate.schedule(`overflow-${index}`, 1);
  }
  check('overflow_still_keeps_one_timer', timers.size === 1);
  runLatestTimer();
  await Promise.resolve();
  check('budget_overflow_forces_authoritative_refresh', refreshes === 2);

  visible = false;
  gate.schedule(null, null);
  runLatestTimer();
  await Promise.resolve();
  check('hidden_flush_has_zero_transport_cost', refreshes === 2);

  visible = true;
  visibilityFailure = true;
  gate.schedule(null, null);
  runLatestTimer();
  visibilityFailure = false;
  check('visibility_failure_is_contained', refreshes === 2
    && warnings.some((message) => message.includes('visibility failure')));

  refreshFailure = 'sync';
  gate.schedule(null, null);
  runLatestTimer();
  refreshFailure = 'async';
  gate.schedule(null, null);
  runLatestTimer();
  await Promise.resolve();
  await Promise.resolve();
  check('refresh_failures_are_contained', refreshes === 4
    && warnings.filter((message) => message.includes('refresh failure')).length === 2);

  gate.schedule('pending-at-destroy', 2);
  gate.destroy();
  gate.schedule(null, null);
  check('destroy_cancels_and_rejects_future_work', timers.size === 0);
  check('resource_budget_is_exported',
    CONVERSATION_CATALOG_REVISION_BUDGET === 64);

  console.log(checks.join('\n'));
  if (checks.some((line) => line.startsWith('FAIL'))) process.exitCode = 1;
})().catch((error) => { console.error(error); process.exit(1); });
""".replace("OWNER_PATH", json.dumps(OWNER_BUNDLE))
    result = subprocess.run(
        ["node", "-e", harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, output
    assert output.count("PASS") == 12, output
