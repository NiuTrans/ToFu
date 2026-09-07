"""Behavior contract for typed local catalog-change reconciliation."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
OWNER = (
    ROOT
    / 'frontend/src/conversation/application/conversation-catalog-reconciliation.ts'
)
OWNER_BUNDLE = native_module_path(
    '.native/conversation-catalog-reconciliation.js', OWNER,
)


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_catalog_change_preserves_activity_and_bounds_sidebar_frames():
    harness = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));
const checks = [];
const check = (name, value) => checks.push((value ? 'PASS ' : 'FAIL ') + name);

const catalog = [
  { id: 'busy', updatedAt: 900, busy: true },
  { id: 'idle', updatedAt: 100, busy: false },
];
const broadcasts = [];
const frames = [];
let renders = 0;
let now = 1_000;
const reconcile = createConversationCatalogReconciler({
  readConversations: () => catalog,
  isConversationBusy: (conversation) => conversation.busy,
  compareConversations: (left, right) => right.updatedAt - left.updatedAt,
  publishCatalogInvalidation: (conversationId) => broadcasts.push(conversationId),
  requestSidebarRender: (render) => frames.push(render),
  renderSidebar: () => { renders += 1; },
  now: () => now,
});

reconcile('idle');
check('idle_change_updates_activity_timestamp', catalog[0].updatedAt === 1_000);
check('catalog_is_sorted_after_change', catalog.map((row) => row.id).join(',') === 'idle,busy');
check('change_publishes_only_the_conversation_identity', broadcasts.join(',') === 'idle');
check('live_catalog_schedules_deferred_sidebar_render', frames.length === 1 && renders === 0);

now = 1_001;
reconcile('busy');
check('busy_change_preserves_authoritative_activity_timestamp',
  catalog.find((row) => row.id === 'busy').updatedAt === 900);
now = 9_000;
reconcile('idle');
check('only_one_sidebar_frame_can_be_pending', frames.length === 1);

frames.shift()();
check('scheduled_frame_releases_the_bound_before_rendering', renders === 1);
now = 3_000;
reconcile('idle');
check('exact_refresh_interval_remains_throttled', frames.length === 0);
now = 3_001;
reconcile('idle');
check('later_live_change_schedules_the_next_frame', frames.length === 1);

const timestamps = catalog.map((row) => row.updatedAt).join(',');
reconcile(null);
check('metadata_only_change_does_not_bump_activity',
  catalog.map((row) => row.updatedAt).join(',') === timestamps
    && broadcasts.at(-1) === null
    && frames.length === 1);

console.log(checks.join('\n'));
if (checks.some((line) => line.startsWith('FAIL'))) process.exitCode = 1;
"""
    result = subprocess.run(
        ['node', '-e', harness, OWNER_BUNDLE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (result.stdout or '') + (result.stderr or '')
    assert result.returncode == 0, output
    assert output.count('PASS') == 10, output
