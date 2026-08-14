"""tests/test_frontend_msg_content_version.py — RENDER_CONTRACT Phase 2 guard.

WHY
---
`static/js/ui/chat_render.js::_msgFingerprint` decided whether a message row
repaints by comparing `String(field).length` and a hand-folded catalogue of
sub-tokens. Two consequences (RENDER_CONTRACT.md §0, L1/L2):

  • L1 — an EQUAL-LENGTH content edit produced the SAME length → the surgical
    diff marked the row "unchanged" → the edit never repainted.
  • L2 — every async field the fingerprint did NOT fold (cost / modifiedFileList
    / artifacts) was invisible to the surgical trigger, so it needed the
    SEPARATE `_bgRefreshChat` output-diff path (the "background repaint" scar).

Phase 2 (Invariant 3): the per-message version becomes a CONTENT HASH and folds
the previously-omitted async-provenance fields, so a single surgical trigger
repaints on those changes too. This suite drives the REAL shipped
`_msgFingerprint` (a pure function; also exposed as the contract name
`_msgContentVersion`) and asserts:

  1. an equal-length content edit MOVES the version (hash, not length);
  2. an equal-length thinking edit MOVES the version;
  3. cost landing on the message (msg.cost) MOVES the version;
  4. modifiedFileList landing MOVES the version;
  5. artifacts hydrating (msg._artifacts) MOVE the version;
  6. NEGATIVE: re-computing with no change is STABLE (no needless repaint);
  7. the contract alias `_msgContentVersion` is exposed and agrees.

NEUTER: rewrite the two content folds back to `.length` (the pre-Phase-2
behaviour) and prove an equal-length edit then produces the SAME version — i.e.
the content HASH is what makes L1 hold.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
const NC = process.argv[4] || '';
global.window = global;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
global.t = (k) => String(k || '').split('.').pop();
global._TOOL_DISPLAY = {};

let chatSrc = fs.readFileSync(process.argv[2], 'utf8');
if (NC === 'length') {
  // NEUTER: revert the two content folds to the pre-Phase-2 `.length` compare.
  // The sentinel is the _hashStr call the Phase-2 patch introduces; absent →
  // the patch hasn't landed.
  const before = chatSrc;
  chatSrc = chatSrc
    .replace('_hashStr(msg.content || "")', '(msg.content || "").length')
    .replace('_hashStr(msg.thinking || "")', '(msg.thinking || "").length');
  if (chatSrc === before) {
    console.log('FAIL neuter_not_applied (Phase-2 _hashStr content fold absent)');
    process.exit(0);
  }
}

// translation_indicator.js provides translationFingerprint's dependency chain
// via translation_model.js; load both so _msgFingerprint's call resolves.
(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[3].replace('escape_html.js', 'translation_model.js'), 'utf8'));
try { (0, eval)(fs.readFileSync(process.argv[3].replace('core/escape_html.js', 'ui/translation_indicator.js'), 'utf8')); } catch (e) {}
(0, eval)(chatSrc);  // chat_render.js (real / neutered)

const out = [];
function check(name, cond, extra) { out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : '')); }

if (typeof _msgFingerprint !== 'function') {
  console.log('FAIL fn_exposed _msgFingerprint missing'); console.log(out.join('\n')); process.exit(0);
}
check('fn_exposed', true);

function mkMsg(extra) {
  return Object.assign({ role: 'assistant', content: 'hello world', thinking: 'reasoning' }, extra || {});
}

// ── 1) Equal-length content edit MOVES the version (hash, not length). ──
{
  const a = _msgFingerprint(mkMsg({ content: 'AAAAA' }));
  const b = _msgFingerprint(mkMsg({ content: 'BBBBB' }));  // same length, diff content
  check('equal_length_content_edit_moves', a !== b, 'a=' + a + ' b=' + b);
}

// ── 2) Equal-length thinking edit MOVES the version. ──
{
  const a = _msgFingerprint(mkMsg({ thinking: 'xxxx' }));
  const b = _msgFingerprint(mkMsg({ thinking: 'yyyy' }));
  check('equal_length_thinking_edit_moves', a !== b);
}

// ── 3) Cost landing (msg.cost stamped) MOVES the version. ──
{
  const before = _msgFingerprint(mkMsg());
  const after = _msgFingerprint(mkMsg({ cost: { costCny: 0.42, costUsd: 0.06 } }));
  check('cost_land_moves_version', before !== after);
  // A different cost number moves it again (not just presence).
  const after2 = _msgFingerprint(mkMsg({ cost: { costCny: 0.99, costUsd: 0.14 } }));
  check('cost_value_change_moves_version', after !== after2);
}

// ── 4) modifiedFileList landing MOVES the version (content, not just count). ──
{
  const before = _msgFingerprint(mkMsg());
  const after = _msgFingerprint(mkMsg({ modifiedFileList: ['a.py', 'b.js'] }));
  check('modified_filelist_land_moves', before !== after);
  // Same COUNT, different paths → still moves (folds path content, not length).
  const afterDiff = _msgFingerprint(mkMsg({ modifiedFileList: ['a.py', 'c.ts'] }));
  check('modified_filelist_content_moves', after !== afterDiff);
}

// ── 5) Artifacts hydrating MOVE the version. ──
{
  const before = _msgFingerprint(mkMsg());
  const after = _msgFingerprint(mkMsg({ _artifacts: [{ id: 'art1', name: 'chart' }] }));
  check('artifacts_hydrate_moves', before !== after);
}

// ── 6) NEGATIVE: no change → STABLE (a noisy version would repaint forever). ──
{
  const m = mkMsg({ cost: { costCny: 0.1 }, modifiedFileList: ['x.py'] });
  check('stable_when_unchanged', _msgFingerprint(m) === _msgFingerprint(m));
}

// ── 7) Contract alias exposed + agrees. ──
{
  const okAlias = typeof _msgContentVersion === 'function'
    || (typeof window !== 'undefined' && typeof window._msgContentVersion === 'function');
  check('content_version_alias_exposed', okAlias);
  if (okAlias) {
    const fn = (typeof _msgContentVersion === 'function') ? _msgContentVersion : window._msgContentVersion;
    check('alias_agrees_with_fingerprint', fn(mkMsg()) === _msgFingerprint(mkMsg()));
  }
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_msg_content_version_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, CHAT_RENDER, ESCAPE_HTML, nc],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


def _lines(output):
    return {ln[5:].split(' ')[0]: ln[:4].strip()
            for ln in output.splitlines() if ln[:4].strip() in ('PASS', 'FAIL')}


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_content_version_hashes_content_and_folds_async_fields():
    """Equal-length edits + async provenance (cost / files / artifacts) all move
    the per-message content version, so the single surgical trigger repaints
    them (Invariant 3, L1/L2)."""
    # Stale-test guard: the Phase-2 wiring must be present in the shipped source.
    src = open(CHAT_RENDER, encoding='utf-8').read()
    assert '_hashStr(msg.content || "")' in src, \
        'Phase-2 content hash fold missing from chat_render.js — test stale'
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'content-version failures:\n' + output
    lines = _lines(output)
    for key in ('equal_length_content_edit_moves', 'equal_length_thinking_edit_moves',
                'cost_land_moves_version', 'cost_value_change_moves_version',
                'modified_filelist_land_moves', 'modified_filelist_content_moves',
                'artifacts_hydrate_moves', 'stable_when_unchanged',
                'content_version_alias_exposed', 'alias_agrees_with_fingerprint'):
        assert lines.get(key) == 'PASS', f'{key} not PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_length_compare_misses_equal_length_edit():
    """NEUTER: revert the content folds to `.length`. An equal-length edit then
    produces the SAME version — proving the content HASH is load-bearing for
    L1 (equal-length repaint)."""
    output = _run('length')
    assert 'FAIL neuter_not_applied' not in output, (
        'the Phase-2 `_hashStr` content fold is absent — Phase 2 has not '
        f'landed yet:\n{output}')
    lines = _lines(output)
    # With length-compare restored, the equal-length content/thinking edits must
    # now COLLIDE (same version → no repaint) — the L1 bug reproduced.
    assert lines.get('equal_length_content_edit_moves') == 'FAIL', (
        'length-compare did NOT collide equal-length content edits — the '
        f'content hash is not load-bearing:\n{output}')
    assert lines.get('equal_length_thinking_edit_moves') == 'FAIL', (
        f'length-compare did NOT collide equal-length thinking edits:\n{output}')
