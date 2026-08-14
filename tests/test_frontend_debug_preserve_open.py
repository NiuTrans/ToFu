"""jsdom test: the debug panel must NOT collapse the user's expanded message
blocks when a streaming ``messages_snapshot`` update falls through to a FULL
re-render.

Reported bug: "the debug panel always closes automatically when new content is
generated while I have it expanded." Root cause — ``showMessagesInDebug`` fires
on every ``messages_snapshot`` SSE with ``isUpdate=true``. The incremental path
preserves ``.open`` blocks, but a structural divergence between the initial
(server-reconstructed) snapshot and the growing live wire snapshot trips the
fall-through to the full render (``p.innerHTML = ""``), which wipes every
expanded block + rendered body and snaps scroll to the top.

The fix captures the open blocks' STABLE IDENTITY (data-mid, not positional
data-idx) + tools-open + scroll before the wipe and re-applies them after the
full render — so restoration survives a snapshot that drops/reorders an earlier
message (the compaction/reconcile drift class the mutation paths already resolve
by stable id).

Harness mirrors tests/test_frontend_debug_approx_chip.py. Skips cleanly when
node + jsdom aren't installed.

NEGATIVE CONTROLS (both patch a COPY; the shipped file stays byte-identical):
  • re-apply neutered → the expanded block collapses (the restore is what
    fixes the reported bug).
  • identity restore degraded to POSITIONAL (data-idx) → the drift case
    re-opens the WRONG block, proving keying on stable identity was required.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()
_DEBUG_SRC = os.path.join(JS_DIR, 'core', 'debug_panel.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="debugPanel">' +
  '<div id="debugTitle"></div>' +
  '<div id="debugContent"></div>' +
  '</div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
win.Icon = global.Icon = (name, size) => `<svg data-icon="${name}" width="${size||14}"></svg>`;
win.t = global.t = (k) => k;
win.activeConvId = global.activeConvId = 'conv-1';
win.conversations = global.conversations = [{ id: 'conv-1' }];
win.debugVisible = global.debugVisible = true;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/debug_panel.js (maybe patched)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof showMessagesInDebug !== 'function') {
  console.log('FAIL fn_present showMessagesInDebug not defined');
  console.log(out.join('\n'));
  process.exit(0);
}
const fn = showMessagesInDebug;
check('fn_present', true);

const panel = document.getElementById('debugContent');

// ── Step 1: cold server-reconstructed snapshot (small) — full render ──
const cold = [
  { role: 'system', content: 'SYS' },
  { role: 'user', content: 'hello there' },
];
fn(cold, '2 msgs (server)', false, 'conv-1', undefined, true);

// ── Step 2: user expands the SYSTEM block (data-idx=0) to inspect it ──
const sysBlock = panel.querySelector('.debug-msg-block[data-idx="0"]');
check('sys_block_exists', !!sysBlock);
sysBlock.querySelector('.debug-msg-header').onclick();
check('sys_block_open_after_click', sysBlock.classList.contains('open'));
const bodyRendered = sysBlock.querySelector('.debug-msg-body').dataset.rendered === '1';
check('sys_body_rendered_after_click', bodyRendered);

// ── Step 3: a live wire snapshot arrives with MANY more messages. The count
//   delta ( |newCount - existingCount| > existingCount ) trips the structural
//   fall-through to a FULL re-render. This is the exact "new content streams
//   in" moment the user reports. ──
const live = [
  { role: 'system', content: 'SYS' },
  { role: 'user', content: 'hello there' },
  { role: 'assistant', content: 'thinking...', tool_calls: [{ function: { name: 'read_files' }, arguments: '{}' }] },
  { role: 'tool', tool_call_id: 'tc1', content: 'file contents' },
  { role: 'assistant', content: 'more' },
  { role: 'tool', tool_call_id: 'tc2', content: 'more results' },
  { role: 'assistant', content: 'answer' },
];
fn(live, 'Round 2 · 7条', true, 'conv-1', undefined);

// ── Step 4: the SYSTEM block the user expanded MUST still be open ──
const sysAfter = panel.querySelector('.debug-msg-block[data-idx="0"]');
check('sys_block_still_present', !!sysAfter);
check('EXPANDED_SURVIVES_FULL_RERENDER',
  sysAfter && sysAfter.classList.contains('open'));
// And its body must be (re)rendered so the JSON is visible, not a blank shell.
check('body_rerendered_after_restore',
  sysAfter && sysAfter.querySelector('.debug-msg-body').dataset.rendered === '1'
  && sysAfter.querySelector('.debug-msg-body pre').innerHTML.length > 0);

// ── DRIFT CASE — the reviewer's requirement ──────────────────────────
//  Expand a UNIQUELY-identifiable assistant message that sits at index 4,
//  then send a snapshot where an EARLIER message has been removed (a
//  compaction/reconcile shrinks the list), so that same message now sits at
//  index 3. Identity-based restore must re-open the block by its content, at
//  its NEW position; a positional (data-idx) restore would instead re-open
//  whatever message lands at index 4 (the WRONG one) and leave the intended
//  one collapsed. We tag the target with a unique marker so we can assert by
//  identity, not position.
const MARK = 'UNIQUE-TARGET-9f3a';
const live2 = [
  { role: 'system', content: 'SYS' },
  { role: 'user', content: 'hello there' },
  { role: 'assistant', content: 'thinking...', tool_calls: [{ function: { name: 'read_files' } }] },
  { role: 'tool', tool_call_id: 'tc1', content: 'file contents' },
  { role: 'assistant', content: MARK },          // index 4 — the user expands this
  { role: 'tool', tool_call_id: 'tc2', content: 'more results' },
];
// Full render to lay these out (isUpdate=false so structure is authoritative).
fn(live2, 'Round 3 · 6条', false, 'conv-1', undefined);
const target = panel.querySelector('.debug-msg-block[data-idx="4"]');
check('target_at_idx4', !!target && target._msgRef && target._msgRef.content === MARK);
target.querySelector('.debug-msg-header').onclick();
check('target_open_after_click', target.classList.contains('open'));

// Now a drift snapshot: message #2 (the 'thinking...' assistant) is DROPPED,
// shifting the marked message from index 4 → index 3. Big enough count delta
// vs the FIRST cold render is not needed here — we force a full render by
// passing isUpdate=false to simulate the reconcile-driven structural replace.
const drift = [
  { role: 'system', content: 'SYS' },
  { role: 'user', content: 'hello there' },
  { role: 'tool', tool_call_id: 'tc1', content: 'file contents' },
  { role: 'assistant', content: MARK },          // now index 3
  { role: 'tool', tool_call_id: 'tc2', content: 'more results' },
];
fn(drift, 'Round 3 · reconciled', false, 'conv-1', undefined);

// The block whose CONTENT is the marker must be the one that stays open,
// wherever it now sits. And the block now sitting at the OLD index (4) must
// NOT be spuriously opened.
let openMarked = null, blockAtOldIdx = null;
panel.querySelectorAll('.debug-msg-block').forEach((b) => {
  if (b._msgRef && b._msgRef.content === MARK && b.classList.contains('open')) openMarked = b;
  if (b.dataset.idx === '4') blockAtOldIdx = b;
});
check('CORRECT_BLOCK_OPEN_AFTER_DROP', !!openMarked);
check('marked_moved_to_idx3', !!openMarked && openMarked.dataset.idx === '3');
// The message now at the OLD index 4 is a DIFFERENT one (the tc2 tool) — it
// must NOT have been opened by a positional restore.
check('wrong_block_not_opened',
  !blockAtOldIdx || !blockAtOldIdx.classList.contains('open'));

console.log(out.join('\n'));
"""


def _run(js_path: str) -> str:
    harness = os.path.join(HERE, '_debug_preserve_open_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, js_path, ROOT],
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


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_expanded_blocks_survive_full_rerender():
    output = _run(_DEBUG_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'debug-panel preserve-open failures:\n' + output
    assert output.count('PASS') >= 12, f'expected >=12 PASS lines, got:\n{output}'
    print(output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_without_reapply_the_block_collapses():
    """Patch a COPY of debug_panel.js so the full-render restore loop is a
    no-op (the pre-fix behavior). The expanded block MUST then collapse across
    the fall-through — proving the re-apply logic is what fixes the bug."""
    with open(_DEBUG_SRC, encoding='utf-8') as f:
        src = f.read()
    # Neuter the restore: make the captured open-identity set swallow every add
    # so the re-apply loop never fires (byte-identical to the old collapse
    # behavior). This targets the IDENTITY-keyed capture, not a positional one.
    needle = 'const _openMids = new Set();'
    assert needle in src, 'anchor for neuter not found — did the fix change shape?'
    patched = src.replace(
        needle,
        'const _openMids = new Set(); _openMids.add = function(){ return this; };',
        1,
    )
    assert patched != src, 'neuter did not modify the source'
    tmp = os.path.join(HERE, '_debug_panel_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(patched)
    try:
        output = _run(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    # With the restore neutered, the expanded block must FAIL to stay open.
    assert 'FAIL EXPANDED_SURVIVES_FULL_RERENDER' in output, (
        'neutered build unexpectedly kept the block open — the assertion is '
        'not load-bearing:\n' + output)
    # Shipped file must be untouched.
    with open(_DEBUG_SRC, encoding='utf-8') as f:
        assert f.read() == src, 'shipped debug_panel.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_positional_restore_reopens_wrong_block_on_drift():
    """Reviewer's option-2 disproof: patch a COPY so identity capture/restore
    degrades to POSITIONAL (data-idx) — the pre-review implementation. The
    drift snapshot (an earlier message dropped, marked message shifted 4→3)
    must then re-open the WRONG block (whatever now sits at index 4) and leave
    the intended one collapsed. This proves index-based restore CANNOT pass the
    drift case, i.e. keying on stable identity (option 1) was required."""
    with open(_DEBUG_SRC, encoding='utf-8') as f:
        src = f.read()
    # Make identity == the positional index. capture reads data-mid, restore
    # matches data-mid; rewriting the identity fn to return the block's index
    # reproduces positional restore exactly.
    needle_capture = 'if (b.dataset.mid) _openMids.add(b.dataset.mid);'
    needle_restore = 'if (!b.dataset.mid || !_openMids.has(b.dataset.mid)) return;'
    assert needle_capture in src and needle_restore in src, \
        'anchors for positional neuter not found — did the fix change shape?'
    patched = (src
               .replace(needle_capture,
                        'if (b.dataset.idx) _openMids.add(b.dataset.idx);', 1)
               .replace(needle_restore,
                        'if (!b.dataset.idx || !_openMids.has(b.dataset.idx)) return;', 1))
    assert patched != src, 'positional neuter did not modify the source'
    tmp = os.path.join(HERE, '_debug_panel_positional.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(patched)
    try:
        output = _run(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    # Positional restore: the marked block at its NEW index 3 is NOT re-opened
    # (index 3 was captured, but index 3 originally held the tc1 tool, not the
    # marker) → CORRECT_BLOCK_OPEN fails; the block now at old index 4 IS
    # wrongly opened → wrong_block_not_opened fails. Assert the drift case broke.
    assert ('FAIL CORRECT_BLOCK_OPEN_AFTER_DROP' in output
            or 'FAIL wrong_block_not_opened' in output), (
        'positional restore unexpectedly passed the drift case — identity may '
        'not be load-bearing:\n' + output)
    with open(_DEBUG_SRC, encoding='utf-8') as f:
        assert f.read() == src, 'shipped debug_panel.js must be byte-identical'


if __name__ == '__main__':
    print(_run(_DEBUG_SRC))
