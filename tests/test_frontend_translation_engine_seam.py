"""tests/test_frontend_translation_engine_seam.py — decoupling step 4 guard.

WHY
---
Step 4 severs the last coupling: the translation ENGINE (static/js/translation.js)
must never touch the DOM. Every repaint now goes through the render-layer seam
`emitMessageChanged(convId, idx, msg, detail)` (+ the window-exposed live-preview
painters), all defined in ui/translation_render.js. This locks that in two ways:

1. STATIC — translation.js contains ZERO direct DOM/render calls
   (getElementById / querySelector(All) / .outerHTML / .innerHTML /
   renderMessage( / renderChat( / the four relocated painter *definitions*).
   This is the exact acceptance grep, asserted in-repo so it can't regress.

2. BEHAVIORAL — under jsdom, stub `emitMessageChanged` as a spy and drive the
   engine's apply-done path (`_applyTranslationDone`). Assert the spy fired for
   the message AND that the engine did not mutate the DOM behind the seam
   (a sentinel #msg-N node is left byte-identical). A NEUTER (make the engine
   call the DOM directly again) must flip the DOM-untouched assertion.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
TRANSLATION_JS = os.path.join(JS_DIR, 'translation.js')

# The forbidden DOM/render tokens (the acceptance grep). Comments are stripped
# before matching so a doc-reference to a relocated name doesn't false-positive.
_FORBIDDEN = [
    r'document\.getElementById\(',
    r'\.querySelector\(',
    r'\.querySelectorAll\(',
    r'\.outerHTML',
    r'\.innerHTML',
    r'\brenderMessage\(',
    r'\brenderChat\(',
    r'\bfunction _renderMsgInPlace\(',
    r'\bfunction _patchTranslateLoadingDom\(',
    r'\bfunction _renderStreamingTranslatePreview\(',
    r'\bfunction _applyPartialByRoundToSettled\(',
]


def _strip_comments(src: str) -> str:
    """Remove /* block */ and // line comments so token matches only hit code.

    Delegates to the SINGLE shared implementation (charter #24).

    NOT a pure equivalence swap -- an UPGRADE, measured across all 171 frontend
    JS files: the local ``re.sub(r'//[^\n]*', '', s)`` treats the ``//`` inside
    a string literal such as ``path.startsWith('http://')`` as a comment and
    DELETES the rest of that line, i.e. it destroys real code. 27 of 171 files
    differ for exactly that reason, and in every one of them the shared
    tokenizer preserves code the local regex ate (checked: zero cases where the
    shared pass lost content the local one kept). So this strictly reduces false
    negatives -- a scan can no longer be blinded by a URL.
    """
    from tests._source_scan import strip_comments
    return strip_comments(src, lang='js', inline=True)


def test_translation_engine_has_zero_dom_calls():
    with open(TRANSLATION_JS, encoding='utf-8') as f:
        code = _strip_comments(f.read())
    offenders = []
    for pat in _FORBIDDEN:
        for m in re.finditer(pat, code):
            line = code.count('\n', 0, m.start()) + 1
            offenders.append(f'{pat} @ line ~{line}')
    assert not offenders, (
        'translation.js must make ZERO direct DOM/render calls (decoupling '
        'step 4) — found:\n  ' + '\n  '.join(offenders))


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"><div id="msg-0">SENTINEL</div></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document;
global.setTimeout = win.setTimeout = () => 0;

const out = [];
function check(n, c) { out.push((c ? 'PASS ' : 'FAIL ') + n); }

const MODE = process.argv[4] || '';
win.activeConvId = global.activeConvId = 'c';
win.conversations = global.conversations = [{ id:'c', messages:[] }];
win.saveConversations = global.saveConversations = () => {};
win._patchMessageOnServer = global._patchMessageOnServer = () => {};
win.t = global.t = (k) => k;

// Spy for the render seam. In the real bundle ui/translation_render.js defines
// emitMessageChanged; here we install a spy BEFORE loading the engine so the
// engine's calls are captured and NO real DOM paint happens.
let emitCalls = [];
if (MODE === 'nc_direct_dom') {
  // NEUTER: emitMessageChanged reaches into the DOM directly (the very coupling
  // step 4 removed) — proves the DOM-untouched assertion is load-bearing.
  win.emitMessageChanged = global.emitMessageChanged = (convId, idx, msg, detail) => {
    emitCalls.push({ idx, detail });
    const el = document.getElementById('msg-' + idx);
    if (el) el.textContent = 'MUTATED';
    return true;
  };
} else {
  win.emitMessageChanged = global.emitMessageChanged = (convId, idx, msg, detail) => {
    emitCalls.push({ idx, detail });
    return true;
  };
}

// Load the ENGINE.
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));  // translation.js

check('applyDone_is_defined', typeof _applyTranslationDone === 'function');

// Drive the apply-done path for msg idx 0.
const msg = { role:'assistant', _msgId:'m0', content:'hello' };
_applyTranslationDone('c', 0, msg, { translated:'你好', model:'x' }, 'translatedContent');

// The engine must have set state AND requested a repaint via the seam.
check('state_applied', msg.translatedContent === '你好' && msg._translateDone === true);
check('emit_called_for_idx0', emitCalls.some(c => c.idx === 0 && c.detail && c.detail.kind === 'full'));

// The engine must NOT have painted the DOM itself — with the real (spy) seam
// the sentinel node is untouched; only the nc_direct_dom neuter mutates it.
const sentinel = document.getElementById('msg-0').textContent;
if (MODE === 'nc_direct_dom') {
  check('nc_dom_was_mutated', sentinel === 'MUTATED');
} else {
  check('dom_untouched_by_engine', sentinel === 'SENTINEL');
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(mode: str = '') -> str:
    harness = os.path.join(HERE, f'_engine_seam_harness_{mode or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, TRANSLATION_JS, ROOT, mode],
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
                    reason='node + jsdom dev-deps not installed')
def test_engine_emits_and_does_not_touch_dom():
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'engine seam behavioral check failed:\n' + output
    assert 'PASS dom_untouched_by_engine' in output
    assert 'PASS emit_called_for_idx0' in output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_nc_direct_dom_mutation_is_detected():
    """Control: a seam that DOES touch the DOM mutates the sentinel — proves the
    dom_untouched assertion above is load-bearing."""
    output = _run('nc_direct_dom')
    assert 'PASS nc_dom_was_mutated' in output, (
        'NEUTER did not mutate the DOM — the untouched assertion is vacuous:\n' + output)
