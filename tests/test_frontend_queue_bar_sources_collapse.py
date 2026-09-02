#!/usr/bin/env python3
"""Queue bar: per-source visual identity + collapse behavior.

WHY
---
Two UX gaps in the input-bar pending queue (``renderPendingQueueUI`` in
``runtime/sections/main/main_send_pipeline.js``):

1.  A flooded queue grew upward and buried the chat transcript. The bar had
    a max-height + internal scroll, but no way to fold it away. Now the
    header carries a chevron toggle; the collapsed state is persisted
    per-conv in localStorage (the bar's innerHTML is rebuilt on every poll,
    so the state cannot live in the DOM) and a queue of
    ``QUEUE_AUTO_COLLAPSE_MIN``+ dispatchable messages auto-collapses until
    the user toggles.

2.  Every queued row looked identical regardless of origin. The data model
    has FIVE sources — ``own`` (typed here), ``agent`` (peer message from a
    sibling conversation), ``operator`` (human operator nudge), ``workflow``
    (Project-Brain epic kickoff, ``kind='workflow_step'``) and the
    ``autopilot`` armed sentinel (which had a CSS class but NO rules at
    all). Each now renders with a ``qsrc-*`` class (tinted left edge +
    number badge in CSS) and, for non-human origins, an attribution line.

This suite EXTRACTS the real shipped helpers + ``renderPendingQueueUI`` +
``togglePendingQueueCollapsed`` and evals them in node against a DOM shim,
asserting the per-source classes/labels and the collapse state machine.

Poisoned NCs: (a) neuter ``_queueSourceOf`` so every non-autopilot item
classifies as ``own`` → all per-source classes + attribution labels vanish;
(b) neuter ``_queueCollapsedNow`` to always expand → the auto-collapse
threshold disappears. Both prove the logic is load-bearing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
SEND_JS = os.path.join(ROOT, 'static', 'js', 'main', 'main_send_pipeline.js')
CSS = os.path.join(ROOT, 'static', 'styles.css')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _brace_match(src: str, open_pos: int) -> int:
    depth = 0
    j = open_pos
    while j < len(src):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise AssertionError('unbalanced braces')


def _node() -> str:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')
    return node


def _extract_queue_block(src: str, *, poison: str = '') -> str:
    """The queue source/collapse helpers + toggle + renderPendingQueueUI as
    ONE block (the render delegates to the helpers, so extracting the bare
    function would ReferenceError under eval)."""
    start = src.index('/* ── Queue item sources')
    m = re.search(r'function\s+renderPendingQueueUI\s*\(', src)
    assert m and m.start() > start
    i = src.find('{', m.end())
    block = src[start:_brace_match(src, i)]
    if poison == 'sources':
        neutered = block.replace(
            "if (item.kind === 'workflow_step') return 'workflow';", '')
        neutered = neutered.replace(
            "if (item.isPeerMessage) return item.isPeerHuman ? 'operator' : 'agent';", '')
        assert neutered != block, 'NC poison did not apply to _queueSourceOf'
        block = neutered
    elif poison == 'collapse':
        neutered = block.replace(
            'return pref !== null ? pref : realCount >= QUEUE_AUTO_COLLAPSE_MIN;',
            'return false;')
        assert neutered != block, 'NC poison did not apply to _queueCollapsedNow'
        block = neutered
    return block


_HARNESS_PREAMBLE = r'''
const _i18n = {
  'queue.messagesQueued': '条消息排队中',
  'queue.clearAll': '全部清空',
  'queue.attachment': '(attachment)',
  'queue.imagesCount': '{n} images',
  'queue.fromConv': 'from',
  'queue.fromOperator': 'from operator',
  'queue.fromWorkflow': 'Brain dispatch',
  'queue.collapse': 'Collapse queue',
  'queue.expand': 'Expand queue',
  'queue.syncedToServer': 'synced',
  'queue.cancelMsg': 'cancel',
  'autopilot.pendingTakeover': 'Autopilot will take over',
  'autopilot.cancelTakeover': 'Cancel autopilot',
  'autopilot.armedShort': 'Autopilot armed',
};
function t(k, args) {
  let s = _i18n[k] != null ? _i18n[k] : k;
  if (args && typeof args === 'object') {
    for (const [kk, vv] of Object.entries(args)) s = s.replace('{' + kk + '}', vv);
  }
  return s;
}
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; });
}
const _TITLES = { mradmzmd: 'Agent Chat', operatorc0nv99: 'Ops Room' };
function convTitleById(cid) { return _TITLES[cid] || 'Untitled chat'; }

// In-memory localStorage shim (collapse pref persistence).
const _store = {};
global.localStorage = {
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
  removeItem: (k) => { delete _store[k]; },
};

// Minimal DOM shim: element registry keyed by id.
const _els = {};
global.document = {
  getElementById: (id) => _els[id] || null,
  createElement: (tag) => {
    const el = {
      tagName: tag, _id: '', className: '', _html: '',
      classList: {
        _c: new Set(),
        add(x) { this._c.add(x); },
        remove(x) { this._c.delete(x); },
        contains(x) { return this._c.has(x); },
      },
      get id() { return this._id; },
      set id(v) { this._id = v; if (v) _els[v] = this; },
      get innerHTML() { return this._html; },
      set innerHTML(v) { this._html = v; },
      appendChild(child) { this._child = child; child.parentNode = this; },
      remove() { if (this.parentNode) this.parentNode._child = null; this.parentNode = null;
                 for (const k of Object.keys(_els)) if (_els[k] === this) delete _els[k]; },
      parentNode: null,
    };
    return el;
  },
};
_els['pendingQueueContainer'] = global.document.createElement('div');
global.setTimeout = (fn, _ms) => { /* no-op — removal timing not under test */ };

var activeConvId = 'c1';
var pendingMessageQueue = new Map();

function _mkReal(text, id) {
  return { queueId: id || 'q-' + text, kind: 'real', text: text,
           images: [], pdfTexts: [], convRefs: [], replyQuotes: [] };
}
function _bar() { return document.getElementById('pendingQueueBar'); }
function _state() {
  const bar = _bar();
  return { html: bar ? bar.innerHTML : '',
           collapsed: bar ? bar.classList.contains('queue-collapsed') : null,
           pref: _store['tofu.queueCollapsed.c1'] ?? null };
}
'''


def _run(driver: str, *, poison: str = '') -> dict:
    node = _node()
    block = _extract_queue_block(_read(SEND_JS), poison=poison)
    src = (_HARNESS_PREAMBLE + '\n' + block + '\n' + driver
           + '\nprocess.stdout.write(JSON.stringify(_state()));\n')
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(src)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, f'node eval failed: {out.stderr}\n---\n{src}'
        return json.loads(out.stdout)
    finally:
        os.unlink(tmp)


# ─────────────────────── per-source identity ───────────────────────

_MIXED_DRIVER = r'''
pendingMessageQueue.set('c1', [
  _mkReal('my own follow-up', 'q1'),
  { queueId: 'q2', kind: 'peer_msg', text: 'agent says hi',
    isPeerMessage: true, fromConv: 'mradmzmd', isPeerHuman: false,
    images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
  { queueId: 'q3', kind: 'peer_msg', text: 'operator nudge',
    isPeerMessage: true, fromConv: 'operatorc0nv99', isPeerHuman: true,
    images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
  { queueId: 'q4', kind: 'workflow_step', text: 'epic kickoff body',
    images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
  { queueId: 'q5', kind: 'autopilot', text: '',
    images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
]);
renderPendingQueueUI('c1');
'''


def test_each_source_gets_a_distinct_class_and_attribution():
    """A mixed queue renders one qsrc-* class per origin, with the peer /
    workflow rows carrying an attribution line; dispatchable rows are
    numbered 1..4 and the autopilot sentinel stays unnumbered."""
    st = _run(_MIXED_DRIVER)
    html = st['html']
    for cls in ('qsrc-own', 'qsrc-agent', 'qsrc-operator',
                'qsrc-workflow', 'pending-queue-autopilot'):
        assert cls in html, f'missing per-source class {cls}'
    assert 'from «Agent Chat»' in html          # agent attribution (title!)
    assert 'from operator «Ops Room»' in html   # operator attribution
    assert 'Brain dispatch' in html             # workflow attribution
    for n in (1, 2, 3, 4):
        assert f'queue-item-number">{n}</span>' in html, f'missing number {n}'
    assert 'queue-item-number">5</span>' not in html  # autopilot unnumbered


def test_workflow_kickoff_is_attributed_and_not_clickable():
    """A brain-dispatch kickoff shows the static workflow attribution line —
    there is no source conversation to jump to, so no loadConversation
    onclick."""
    st = _run('''
pendingMessageQueue.set('c1', [
  { queueId: 'q1', kind: 'workflow_step', text: 'kickoff: implement the parser refactor',
    images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
]);
renderPendingQueueUI('c1');
''')
    assert 'qsrc-workflow' in st['html']
    assert 'queue-item-src-static' in st['html']
    assert 'Brain dispatch' in st['html']
    assert 'loadConversation' not in st['html']


def test_own_message_is_neutral_and_has_no_attribution():
    """A plain typed-here queued message keeps the neutral default look and
    no source line."""
    st = _run('''
pendingMessageQueue.set('c1', [ _mkReal('just a normal message') ]);
renderPendingQueueUI('c1');
''')
    assert 'qsrc-own' in st['html']
    assert 'queue-item-src' not in st['html']
    assert st['collapsed'] is False


# ─────────────────────── collapse behavior ───────────────────────

def test_collapse_toggle_persists_and_survives_rerender():
    """The chevron flips the state, persists it per-conv, and a poll
    re-render re-applies it (the state lives in localStorage, not the DOM)."""
    driver = '''
pendingMessageQueue.set('c1', [ _mkReal('first', 'q1'), _mkReal('second', 'q2') ]);
renderPendingQueueUI('c1');
const before = _state();
togglePendingQueueCollapsed('c1');
const afterToggle = _state();
renderPendingQueueUI('c1');            // simulated poll re-render
const afterRerender = _state();
togglePendingQueueCollapsed('c1');
const afterSecond = _state();
process.stdout.write(JSON.stringify({ before, afterToggle, afterRerender, afterSecond }));
process.exit(0);
'''
    node = _node()
    block = _extract_queue_block(_read(SEND_JS))
    src = _HARNESS_PREAMBLE + '\n' + block + '\n' + driver
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(src)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, f'node eval failed: {out.stderr}'
        r = json.loads(out.stdout)
    finally:
        os.unlink(tmp)
    assert r['before']['collapsed'] is False
    assert r['afterToggle']['collapsed'] is True
    assert r['afterToggle']['pref'] == '1'
    assert r['afterRerender']['collapsed'] is True, (
        'collapse state lost across a poll re-render — it must live in '
        'localStorage, not the DOM'
    )
    assert r['afterSecond']['collapsed'] is False
    assert r['afterSecond']['pref'] == '0'


def test_auto_collapse_threshold_and_explicit_override():
    """4+ dispatchable messages auto-collapse with no pref; 3 stay expanded;
    an explicit expand pref defeats the auto rule even at 5."""
    driver = '''
function mkReals(n) { const a = []; for (let i = 0; i < n; i++) a.push(_mkReal('m' + i, 'q' + i)); return a; }
pendingMessageQueue.set('c1', mkReals(4));
renderPendingQueueUI('c1');
const four = _state();
pendingMessageQueue.set('c1', mkReals(3));
renderPendingQueueUI('c1');
const three = _state();
localStorage.setItem('tofu.queueCollapsed.c1', '0');
pendingMessageQueue.set('c1', mkReals(5));
renderPendingQueueUI('c1');
const fiveOverride = _state();
process.stdout.write(JSON.stringify({ four, three, fiveOverride }));
process.exit(0);
'''
    node = _node()
    block = _extract_queue_block(_read(SEND_JS))
    src = _HARNESS_PREAMBLE + '\n' + block + '\n' + driver
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(src)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, f'node eval failed: {out.stderr}'
        r = json.loads(out.stdout)
    finally:
        os.unlink(tmp)
    assert r['four']['collapsed'] is True, 'a 4-message flood must auto-collapse'
    assert r['three']['collapsed'] is False, '3 messages must stay expanded'
    assert r['fiveOverride']['collapsed'] is False, (
        'the explicit expand toggle must win over the auto-collapse rule'
    )


def test_collapsed_header_shows_next_preview_and_autopilot_chip():
    """Collapsed, the bar is one line: count + a next-up preview of the FIRST
    dispatchable message + (when armed) the autopilot chip, so no state is
    illegible while the list is folded."""
    st = _run('''
pendingMessageQueue.set('c1', [
  _mkReal('NEXT-UP-MESSAGE', 'q1'),
  _mkReal('m2', 'q2'), _mkReal('m3', 'q3'), _mkReal('m4', 'q4'),
  { queueId: 'q9', kind: 'autopilot', text: '',
    images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
]);
renderPendingQueueUI('c1');
''')
    assert st['collapsed'] is True
    assert re.search(r'queue-next-preview[^>]*>NEXT-UP-MESSAGE', st['html']), (
        'collapsed header must carry the next-up preview'
    )
    assert 'queue-header-ap' in st['html'], (
        'the autopilot armed state must stay visible in the collapsed header'
    )
    # The collapse toggle itself is always rendered.
    assert 'queue-toggle' in st['html']


def test_expanded_header_label_keeps_flex_grow_static_guard():
    """The clear-all button is pushed to the header's right edge by the
    LABEL's flex-grow — in the EXPANDED state the only other grow candidate
    (the next-up preview) is display:none, so dropping the grow strands
    clear-all against the count text (owner-caught regression in d1f28583).
    Static guard on comment-stripped styles.css: a .queue-header-label rule
    OUTSIDE any .queue-collapsed scope must carry flex-grow >= 1, and the
    collapsed override that hands the grow to the preview must exist."""
    css = re.sub(r'/\*.*?\*/', '', _read(CSS), flags=re.S)   # strip comments
    found_grow = False
    found_collapsed_override = False
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        selector, body = m.group(1), m.group(2)
        if not re.search(r'queue-header-label(?![\w-])', selector):
            continue
        grow = re.search(r'(?:^|;)\s*flex\s*:\s*(\d+)', body)
        if 'queue-collapsed' in selector:
            found_collapsed_override = True
        elif grow and int(grow.group(1)) >= 1:
            found_grow = True
    assert found_grow, (
        'no non-collapsed .queue-header-label rule with flex-grow — the '
        'expanded header loses its spacer and clear-all drifts off the '
        'right edge'
    )
    assert found_collapsed_override, (
        'the collapsed state must explicitly hand the grow back to the '
        'next-up preview via a .queue-collapsed .queue-header-label override'
    )


# ─────────────────────── poisoned NCs (load-bearing) ───────────────────────

def test_nc_neutered_source_classification_erases_every_distinction():
    """Neuter _queueSourceOf (all non-autopilot → 'own') → every per-source
    class and attribution label disappears, proving the classification is
    what produces the distinct visuals."""
    st = _run(_MIXED_DRIVER, poison='sources')
    html = st['html']
    for cls in ('qsrc-agent', 'qsrc-operator', 'qsrc-workflow'):
        assert cls not in html, f'NC: {cls} survived the neutered classifier'
    assert 'from «Agent Chat»' not in html
    assert 'Brain dispatch' not in html


def test_nc_neutered_collapse_state_never_collapses():
    """Neuter _queueCollapsedNow (always expand) → the 4-message flood no
    longer auto-collapses, proving the threshold logic is load-bearing."""
    st = _run('''
pendingMessageQueue.set('c1', [
  _mkReal('m1', 'q1'), _mkReal('m2', 'q2'), _mkReal('m3', 'q3'), _mkReal('m4', 'q4'),
]);
renderPendingQueueUI('c1');
''', poison='collapse')
    assert st['collapsed'] is False, (
        'NC: with _queueCollapsedNow neutered the flood still collapsed — '
        'the threshold is not what drives the behavior'
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
