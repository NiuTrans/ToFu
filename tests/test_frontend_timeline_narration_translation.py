"""Frontend: settled segment-timeline narration renders per-round translation.

Symptom the fix addresses: after the conv-open cache-recovery fix (segments
re-render on open), the interleaved tool/thinking timeline reappears — but the
inter-round NARRATION prose ("Let me check the files.") stays ENGLISH even when
auto-translate is on. Root cause: `_renderTimelineBatch` (static/js/ui/
tool_rounds.js) rendered narration as `renderMarkdown(s.text)` — it never read
`s.translatedText`, which the backend incremental translator DOES stamp onto
each non-deliverable text segment (lib/translate/commit.py::
_stamp_segment_translations, keyed by llmRound).

Fix: the narration branch now prefers `s.translatedText` (stripped of
notranslate markers, guarded) when non-empty, else falls back to `s.text`.

NOTE — thinking segments are intentionally NOT covered: the backend translator
only ever stamps `translatedText` on non-deliverable `text` (narration)
segments (commit.py:54-63 / runtime.py:150 skip non-text), so a thinking branch
reading translatedText would be permanently inert. This test therefore asserts
narration only.

Runs the REAL shipped `_renderTimelineBatch` under node (brace-extracted, deps
stubbed). 1 assertion + 1 biting negative control. Skips when node is absent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
TR_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'tool_rounds.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_fn(src: str, name: str) -> str:
    """Brace-match a top-level `function <name>(...) { ... }`."""
    m = re.search(r'function %s\s*\(' % re.escape(name), src)
    assert m, f'{name} not found'
    i = src.index('{', m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f'unbalanced braces extracting {name}')


def _run_node(script: str) -> dict:
    out = subprocess.run(['node', '-e', script], capture_output=True,
                         text=True, cwd=ROOT, timeout=60)
    assert out.returncode == 0, f'node failed: {out.stderr}\n---\n{out.stdout}'
    last = [ln for ln in out.stdout.strip().splitlines()
            if ln.strip().startswith('{')][-1]
    return json.loads(last)


# Deliberately-transparent stubs: renderMarkdown / escapeHtml echo input so we
# can assert on the raw rendered text; stripNoTranslateTags is identity;
# _renderToolGroupsHTML / t are no-ops. This isolates the segment-text SELECTION
# (text vs translatedText) — the only behaviour under test.
_HARNESS = r"""
'use strict';
function renderMarkdown(s) { return String(s == null ? '' : s); }
function escapeHtml(s) { return String(s == null ? '' : s); }
function stripNoTranslateTags(s) { return s; }
function _renderToolGroupsHTML() { return '<TOOLS/>'; }
function t() { return 'thinking'; }

__FN__

// A batch with ONE non-deliverable narration text segment carrying a per-round
// Chinese translatedText, plus a tool_use round for realism.
const batch = [
  { type: 'text', deliverable: false, llmRound: 1,
    text: 'Let me check the files.', translatedText: '让我检查一下这些文件。' },
];
const rounds = [];  // no rich tool rounds needed for narration assertion
const html = _renderTimelineBatch(batch, rounds, [], 0);
console.log(JSON.stringify({
  html: html,
  hasZh: html.indexOf('让我检查一下这些文件。') !== -1,
  hasEn: html.indexOf('Let me check the files.') !== -1,
}));
"""


def _run(neuter: bool = False) -> dict:
    src = open(TR_JS, encoding='utf-8').read()
    fn = _extract_fn(src, '_renderTimelineBatch')
    if neuter:
        # NC: revert the narration selection back to raw s.text (the pre-fix
        # behaviour). Matches the fixed `const _segText = (... ) ? ... : s.text;`
        # line and forces it to English so the branch is proven load-bearing.
        neutered = re.sub(
            r'const _segText = \(s\.translatedText.*?: s\.text;',
            'const _segText = s.text;',
            fn, count=1, flags=re.S)
        assert neutered != fn, 'NC did not alter the narration selection'
        fn = neutered
    return _run_node(_HARNESS.replace('__FN__', fn))


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_narration_renders_translated_text():
    """REAL _renderTimelineBatch: a narration segment with translatedText
    renders the Chinese (not the English source)."""
    r = _run(neuter=False)
    assert r['hasZh'], f'expected Chinese narration in timeline: {r}'
    assert not r['hasEn'], f'English source leaked despite translation: {r}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_nc_without_translatedtext_branch_renders_english():
    """NC: reverting the selection to raw s.text renders the ENGLISH source —
    proving the translatedText preference is the load-bearing change."""
    r = _run(neuter=True)
    assert r['hasEn'], f'NC did not bite — expected English fallback: {r}'
    assert not r['hasZh'], f'NC still showed Chinese unexpectedly: {r}'


if __name__ == '__main__':
    print(_run(False))
    print(_run(True))
