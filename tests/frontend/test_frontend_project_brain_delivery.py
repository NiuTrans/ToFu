"""Delivery labels execute the retained owner without a build or DOM package.

The jsdom sibling exercises browser wiring. This narrow spec uses a minimal
host port to prove delivery evidence is escaped and independent of candidate
merge state, including historical payloads without delivery metadata.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / 'frontend/src/runtime/sections/project-brain-integration.js'


def test_delivery_projection_uses_authoritative_state_not_candidate_merge():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    result = subprocess.run([node, '-e', r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const host = {innerHTML: '', addEventListener() {}};
const context = {
  runtimeScope: {}, window: {addEventListener() {}},
  document: {getElementById: id => id === 'projectBrainIntegrationBody' ? host : null},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const render = context.runtimeScope.ProjectBrainIntegration.renderIntegration;
const labels = {
  pending: 'Current checkout: delivery pending',
  blocked: 'Current checkout: delivery blocked',
  delivered: 'Delivered to current checkout',
  preserved: 'Current checkout preserved; not delivered',
  failed: 'Current checkout: delivery failed',
  unknown: 'Current checkout: delivery not verified',
};
for (const state of ['pending', 'blocked', 'delivered', 'preserved', 'failed', null, 'bogus']) {
  const delivery = state ? {
    state, reason: '<script>not executable</script>',
    headSha: 'head123', targetSha: 'target456', updatedAt: '2026-09-29T00:00:00Z',
  } : null;
  render({refs: {}, workspaces: [{state: 'merged', delivery}]});
  const html = host.innerHTML;
  assert(html.includes('Merged into candidate'));
  assert(html.includes(labels[state] || labels.unknown));
  assert.strictEqual(html.includes('Delivered to current checkout'), state === 'delivered');
  assert(!html.includes('<script>'));
  if (delivery) {
    assert(html.includes('&lt;script&gt;not executable&lt;/script&gt;'));
    assert(html.includes('head123') && html.includes('target456'));
  }
}
context.t = key => ({
  'projectBrain.integration.state.candidateMerged': '候选已合并',
  'projectBrain.integration.delivery.blocked': '当前代码交付受阻',
}[key] || key);
render({refs: {}, workspaces: [{state: 'merged', delivery: {state: 'blocked'}}]});
assert(host.innerHTML.includes('候选已合并'));
assert(host.innerHTML.includes('当前代码交付受阻'));
console.log('7 delivery cases and localization seam passed');
''', str(OWNER)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '7 delivery cases and localization seam passed' in result.stdout
