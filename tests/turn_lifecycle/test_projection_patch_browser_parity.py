"""The browser consumes real producer patches without mutating source evidence."""
import copy
import json
from pathlib import Path
import subprocess

import pytest

from lib.turn_projection_patch import apply_projection_patch, build_projection_patch
from tests._jsdom import require_node_modules
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_python_generated_batches_keep_browser_projection_and_immutability():
    require_node_modules('vite')
    bundle = native_module_path('.native/projection-patch-parity.js',
                                ROOT / 'frontend/src/core/projection-patch.ts')
    cases = []
    for count, changed in [(8, 1), (512, 1), (4096, 32)]:
        before = {'content': 'answer', 'toolRounds': [
            {'toolId': f'call-{index}', 'toolContent': f'output {index}',
             'status': 'running', 'success': False}
            for index in range(count)]}
        after = copy.deepcopy(before)
        for row in after['toolRounds'][-changed:]:
            row.update(toolContent=row['toolContent'] + ' complete',
                       status='completed', success=True)
        patch = build_projection_patch(before, after, base_revision=1, target_revision=2)
        assert apply_projection_patch(before, patch) == after
        cases.append({'before': before, 'after': after, 'patch': patch})

    script = r'''
const fs = require('node:fs'), assert = require('node:assert/strict');
eval(fs.readFileSync(process.argv[1], 'utf8'));
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze); Object.freeze(value);
  }
  return value;
}
for (const fixture of cases) {
  const original = JSON.stringify(fixture);
  freeze(fixture);
  assert.deepEqual(applyProjectionPatch(fixture.before, fixture.patch), fixture.after);
  assert.equal(JSON.stringify(fixture), original);
}
console.log(JSON.stringify({ verified: cases.length }));
'''
    result = subprocess.run(['node', '-e', script, bundle], cwd=ROOT,
                            input=json.dumps(cases), text=True, capture_output=True,
                            timeout=60, check=True)
    assert json.loads(result.stdout) == {'verified': len(cases)}
