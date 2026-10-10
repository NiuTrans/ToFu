"""Device layout migration preserves choices without persisting mere opening."""
import subprocess
from pathlib import Path

import pytest
from tests._runtime_sections import _require_frontend_bundler

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_preview_layout_migrates_default_and_retains_deliberate_preferences(tmp_path):
    bundle = tmp_path / 'layout.cjs'
    # Shared dep guard: skip on dep-less lanes, fail under
    # TOFU_REQUIRE_FRONTEND=1 (was an unguarded bundler call).
    _require_frontend_bundler()
    subprocess.run(['node', 'scripts/vite_test_bundle.mjs', 'frontend/src/features/paper/preview-layout.ts',
                    '--bundle', '--format=cjs', '--platform=node', '--outfile=' + str(bundle)],
                   cwd=ROOT, check=True, capture_output=True)
    script = r'''
const assert=require('node:assert/strict');
const values=new Map();let writes=0;
globalThis.localStorage={getItem:key=>values.get(key)??null,setItem:(key,value)=>{writes++;values.set(key,value)},removeItem:key=>values.delete(key)};
const layout=require(process.argv[1]);
assert.equal(layout.readPreviewSplit(),40);assert.equal(writes,0);
values.set('tofu_paper_preview_split','55');
assert.equal(layout.readPreviewSplit(),40);assert.equal(values.has('tofu_paper_preview_split'),false);
values.clear();values.set('tofu_paper_preview_split','46');
assert.equal(layout.readPreviewSplit(),46);
layout.persistPreviewSplit(55);assert.equal(layout.readPreviewSplit(),55);
layout.persistPreviewSplit(NaN);assert.equal(layout.readPreviewSplit(),40);
values.set('tofu_paper_preview_layout','{malformed');assert.equal(layout.readPreviewSplit(),40);
values.set('tofu_paper_preview_layout','x'.repeat(129));assert.equal(layout.readPreviewSplit(),40);
globalThis.localStorage={getItem(){throw Error('denied')},setItem(){throw Error('quota')}};
assert.equal(layout.readPreviewSplit(),40);assert.equal(layout.persistPreviewSplit(42),false);
'''
    result = subprocess.run(['node', '-e', script, str(bundle)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
