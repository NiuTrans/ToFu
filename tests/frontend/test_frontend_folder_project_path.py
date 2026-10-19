"""tests/frontend/test_frontend_folder_project_path.py — Folder default project roots.

WHY
---
Folders (the sidebar project rail) can bind DEFAULT project roots. A brand-new
conversation created inside a folder inherits every bound root (first = primary,
rest = extra roots) so the user does not re-enter them per chat. The binding
lives on the folder record (``folder.projectPaths[]``, with ``projectPath`` kept
as the primary-root scalar mirror); the frontend resolver
``folderDefaultProjectPaths(folderId)`` in ``core/folders.js`` returns the
ordered, de-duplicated roots, the first of which is the primary project.

This spec evals the REAL retained ``core/folders.js`` under a minimal scope and
asserts the resolver contract:

  • ``projectPaths[]`` is returned trimmed, de-duplicated, in order,
  • a legacy scalar ``projectPath`` folds into a one-element list,
  • unknown / null ids and folders with no usable roots yield [] / '',
  • non-string / blank entries are dropped without throwing.

Skips cleanly without node.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))


def _node_available() -> bool:
    return bool(shutil.which('node'))


_FOLDERS_SECTION = runtime_section_path('core/folders.js', scope_prelude=True)

_HARNESS = r"""
const fs = require('fs');
const SECTION = process.env.FOLDERS_SECTION;

// ── Minimal window-scope for the retained section (scope_prelude adds
//    `var runtimeScope = window`). ──
global.window = globalThis;

// ── Folder state is driven through the section's OWN seam: Api.folders.list
//    feeds loadFolders(), which populates the internal _folders array that
//    getFolderById + folderDefaultProjectPath read. ──
const ROWS = [
  { id: 'f1', name: 'multi', color: '#3b82f6', order: 0,
    projectPath: '~/Projects/tofu',
    projectPaths: ['  ~/Projects/tofu  ', '/mnt/data/ref', '~/Projects/tofu', 42, ''] },
  { id: 'f2', name: 'legacy', color: '#10b981', order: 1,
    projectPath: '  ~/old/proj  ' },
  { id: 'f3', name: 'blank', color: '#ef4444', order: 2,
    projectPath: '   ', projectPaths: [] },
  { id: 'f4', name: 'weird', color: '#8b5cf6', order: 3, projectPath: 42 },
];
global.Api = { folders: {
  list: async () => ROWS,
  create: async () => null, update: async () => null, remove: async () => false,
} };
global.renderConversationList = () => {};

global._folders = [];
global._foldersLoaded = false;

(0, eval)(fs.readFileSync(SECTION, 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

async function main() {
  await loadFolders();   // hydrate the section's internal _folders from ROWS

// plural resolver: trimmed, de-duplicated, ordered, junk dropped
const p1 = folderDefaultProjectPaths('f1');
check('plural_multi_roots',
  Array.isArray(p1) && p1.length === 2
  && p1[0] === '~/Projects/tofu' && p1[1] === '/mnt/data/ref');
// legacy scalar folds into a single-element list
check('plural_legacy_scalar',
  JSON.stringify(folderDefaultProjectPaths('f2')) === JSON.stringify(['~/old/proj']));
// empty / unknown / junk
check('plural_blank_is_empty',
  JSON.stringify(folderDefaultProjectPaths('f3')) === '[]');
check('plural_unknown_is_empty',
  JSON.stringify(folderDefaultProjectPaths('nope')) === '[]');
check('plural_null_is_empty',
  JSON.stringify(folderDefaultProjectPaths(null)) === '[]');
check('plural_nonstring_is_empty',
  JSON.stringify(folderDefaultProjectPaths('f4')) === '[]');

// primary root is the first element
check('primary_is_first', folderDefaultProjectPaths('f1')[0] === '~/Projects/tofu');
}

main().then(
  () => { console.log(out.join('\n')); },
  (e) => { console.error('harness error:', e && e.stack || e); process.exit(1); },
);
"""

_EXPECTED = (
    'plural_multi_roots', 'plural_legacy_scalar', 'plural_blank_is_empty',
    'plural_unknown_is_empty', 'plural_null_is_empty', 'plural_nonstring_is_empty',
    'primary_is_first',
)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_folder_default_project_path_resolver():
    env = dict(os.environ, FOLDERS_SECTION=_FOLDERS_SECTION)
    proc = subprocess.run(
        ['node', '-e', _HARNESS],
        capture_output=True, text=True, timeout=60, env=env,
    )
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'folderDefaultProjectPath failures:\n' + output
    for must in _EXPECTED:
        assert ('PASS ' + must) in output, output


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
