#!/usr/bin/env python3
"""Peer/queue surfaces render a conversation TITLE, never a raw id.

WHY
---
The Project-Brain peer surfaces carried a bare conversation id where a human
expects a title:

  • the ``project_message`` / ``project_intervene`` delivery card showed
    ``conv mradmzmd`` (an 8-char display id — meaningless to a user), and
  • a queued peer/operator turn in the input bar was labelled by the same
    raw id.

The fix routes every id→title through ONE frontend seam, ``convTitleById``
(``static/js/core/conversations.js``): match the full id, then a UNIQUE prefix
(so an 8-char display id still resolves against the loaded ``conversations``
list), else fall back to a localized "Untitled chat" — NEVER a bare id. The
delivery card (``_renderPeerDelivery`` in ``ui/tool_rounds.js``) and the queue
source line (``renderPendingQueueUI`` in ``main/main_send_pipeline.js``) both
call it.

This test EXTRACTS the real shipped ``convTitleById`` + the two real consumers
and evals them under jsdom with a real ``conversations`` list, asserting the
resolved TITLE appears (not the id) and the id is demoted to the ``title=``
tooltip. It closes the coverage gap where ``test_frontend_brain_tool_render.py``
only passed its ``peermsg_target`` check because ``convTitleById`` was UNDEFINED
in that harness (the resolution path was never executed).

Poisoned NC: neuter ``convTitleById``'s lookup so it always returns the fallback
label → both surfaces stop showing the real title, proving the resolution is
load-bearing (not a tautology of the fallback).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
# NOTE: convTitleById is NOT looked up by a hard-coded path. It started life in
# core/conversations.js and moved to core/conv_reducers.js in the decomposition;
# a pinned path turns that legitimate refactor into `convTitleById not found`,
# which reads like the seam was deleted. Resolve it by SYMBOL from the
# production bundle manifests so the next slice carries this guard with it.
TR_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'tool_rounds.js')
SEND_JS = os.path.join(ROOT, 'static', 'js', 'main', 'main_send_pipeline.js')


def _src_defining(symbol: str) -> str:
    """Absolute path of the shipped file that defines *symbol*.

    Raises with a four-state diagnosis (gone / unbundled / duplicated /
    resolved) rather than a bare 'not found'.
    """
    from tests._conv_bundle_sources import sources_defining
    return sources_defining(symbol)[-1]


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


def _extract_fn(src: str, fn_name: str) -> str:
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(fn_name) + r'\s*\(', src)
    assert m, f'{fn_name} not found'
    i = src.find('{', m.end())
    return src[m.start():_brace_match(src, i)]


def _node() -> str:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')
    return node


def _extract_queue_block(src: str) -> str:
    """The queue source/collapse helpers + renderPendingQueueUI as ONE block.

    renderPendingQueueUI now delegates to the source/collapse helpers
    (``_queueSourceOf``, ``_queueCollapsedNow``, the ``_QUEUE_*`` icon
    consts, …), so extracting the bare function would ReferenceError under
    eval. The block spans from the sources marker through the end of the
    render function (the collapse toggle is defined in between)."""
    start = src.index('/* ── Queue item sources')
    m = re.search(r'function\s+renderPendingQueueUI\s*\(', src)
    assert m and m.start() > start
    i = src.find('{', m.end())
    return src[start:_brace_match(src, i)]


# The conversations the harness pretends are loaded. The peer ids the backend
# surfaces are the 8-char display form; the real rows are 14 chars — so a match
# MUST succeed by unique prefix.
_CONVS = [
    {'id': 'mradmzmdxyz123', 'title': 'Segment timeline prefill-resume'},
    {'id': 'operatorc0nv99', 'title': 'Operator control room'},
    {'id': 'zzzznomatch0000', 'title': 'Unrelated conversation'},
]


def _harness(*, extracted: str, driver: str, lang: str = 'en') -> str:
    return f'''
const _i18nTable = {{
  'toast.untitledConv': {{ zh: '未命名对话', en: 'Untitled chat' }},
  'queue.fromConv': {{ zh: '来自', en: 'from' }},
  'queue.fromOperator': {{ zh: '来自操作员', en: 'from operator' }},
  'queue.messagesQueued': {{ zh: '条消息排队中', en: 'messages queued' }},
  'queue.clearAll': {{ zh: '全部清空', en: 'Clear all' }},
  'projectBrain.pdMessage': {{ zh: '发送消息', en: 'Message' }},
}};
const _lang = {json.dumps(lang)};
function t(k, d) {{
  const e = _i18nTable[k];
  if (e && e[_lang] != null) return e[_lang];
  return d != null ? d : k;
}}
function escapeHtml(s) {{ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){{
  return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]; }}); }}
function Icon(name) {{ return '<svg data-ico="' + name + '"></svg>'; }}

// A real "loaded conversations" list — convTitleById reads this global.
var conversations = {json.dumps(_CONVS)};

// Minimal jsdom-free DOM shim for renderPendingQueueUI: it only needs
// getElementById + createElement + appendChild + innerHTML.
const _els = {{}};
global.document = {{
  getElementById: (id) => _els[id] || null,
  createElement: (tag) => {{
    const el = {{ tagName: tag, _id: '', className: '', _html: '',
      classList: {{ add() {{}}, remove() {{}} }},
      // Registering on id-set mirrors "in the DOM, retrievable by id" — the
      // real renderPendingQueueUI sets container.id='pendingQueueBar' then
      // relies on getElementById finding it on the next render.
      get id() {{ return this._id; }},
      set id(v) {{ this._id = v; if (v) _els[v] = this; }},
      get innerHTML() {{ return this._html; }},
      set innerHTML(v) {{ this._html = v; }},
      appendChild(child) {{ this._child = child; child.parentNode = this; }},
      parentNode: null }};
    return el;
  }},
}};
// Pre-create the queue host so appendChild lands somewhere retrievable.
_els['pendingQueueContainer'] = global.document.createElement('div');

// pendingMessageQueue is the Map renderPendingQueueUI reads.
var pendingMessageQueue = new Map();
// renderPendingQueueUI now gates DOM mutations on activeConvId — declare it so
// the queue-source-line assertions still see the paint. See
// test_frontend_pending_queue_active_conv_gate.py for the cross-conv guard.
var activeConvId = 'c1';

{extracted}

{driver}
'''


def _run(harness: str) -> str:
    node = _node()
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, f'node eval failed: {out.stderr}'
        return out.stdout
    finally:
        os.unlink(tmp)


def _extracted(*, poison: bool = False) -> str:
    """The real convTitleById + _renderPeerDelivery + renderPendingQueueUI."""
    conv_src = _read(_src_defining('convTitleById'))
    tr_src = _read(TR_JS)
    send_src = _read(SEND_JS)
    fn_title = _extract_fn(conv_src, 'convTitleById')
    if poison:
        # Neuter the lookup: force the fallback branch by making the match
        # loop see no conversations. This must strip the real title from BOTH
        # consumers → they show the localized fallback instead.
        neutered = fn_title.replace(
            'if (typeof conversations !== \'undefined\' && Array.isArray(conversations)) {',
            'if (false) {')
        assert neutered != fn_title, 'NC poison did not apply to convTitleById'
        fn_title = neutered
    fn_delivery = _extract_fn(tr_src, '_renderPeerDelivery')
    fn_queue = _extract_queue_block(send_src)
    return '\n'.join([fn_title, fn_delivery, fn_queue])


# ─────────────────────── delivery card resolves title ───────────────────────

def test_delivery_card_shows_title_not_id():
    """_renderPeerDelivery renders the resolved TITLE; the short id is demoted
    to the title= tooltip only."""
    driver = '''
const pd = { tool: 'project_message', toConv: 'mradmzmd',
             text: 'Watch out for the overlap', outcome: 'delivered' };
process.stdout.write(_renderPeerDelivery(pd));
'''
    html = _run(_harness(extracted=_extracted(), driver=driver))
    # Resolved by unique prefix (mradmzmd → mradmzmdxyz123).
    assert 'Segment timeline prefill-resume' in html
    # The user-facing target span shows the title, NOT "conv mradmzmd".
    assert 'conv mradmzmd' not in html
    # The raw id survives only in the tooltip attribute.
    assert 'title="mradmzmd"' in html


# ─────────────────────── queue source line resolves title ───────────────────────

def test_queue_source_line_shows_title_not_id():
    """renderPendingQueueUI renders 'from «Title»' for a peer turn, not the id."""
    driver = '''
pendingMessageQueue.set('c1', [{
  queueId: 'q1', kind: 'peer_msg', text: 'Done — I shipped the renderer.',
  isPeerMessage: true, fromConv: 'mradmzmd', isPeerHuman: false,
  images: [], pdfTexts: [], convRefs: [], replyQuotes: [],
}]);
renderPendingQueueUI('c1');
process.stdout.write(document.getElementById('pendingQueueBar').innerHTML);
'''
    html = _run(_harness(extracted=_extracted(), driver=driver))
    assert 'queue-item-src' in html
    assert 'from «Segment timeline prefill-resume»' in html
    # The clean message text is shown in full, no raw framing/id leaked.
    assert 'Done — I shipped the renderer.' in html
    assert 'conv mradmzmd' not in html


def test_queue_operator_label_and_title():
    """A human operator nudge uses the 'from operator' label + resolved title."""
    driver = '''
pendingMessageQueue.set('c1', [{
  queueId: 'q1', kind: 'peer_msg', text: 'Please pause and re-check the board.',
  isPeerMessage: true, fromConv: 'operatorc0nv99', isPeerHuman: true,
  images: [], pdfTexts: [], convRefs: [], replyQuotes: [],
}]);
renderPendingQueueUI('c1');
process.stdout.write(document.getElementById('pendingQueueBar').innerHTML);
'''
    html = _run(_harness(extracted=_extracted(), driver=driver))
    assert 'from operator «Operator control room»' in html


def test_plain_real_message_has_no_source_line():
    """A normal human queued turn renders no peer source line."""
    driver = '''
pendingMessageQueue.set('c1', [{
  queueId: 'q1', kind: 'real', text: 'just a normal message',
  images: [], pdfTexts: [], convRefs: [], replyQuotes: [],
}]);
renderPendingQueueUI('c1');
process.stdout.write(document.getElementById('pendingQueueBar').innerHTML);
'''
    html = _run(_harness(extracted=_extracted(), driver=driver))
    assert 'queue-item-src' not in html
    assert 'just a normal message' in html


def test_unknown_conv_falls_back_to_untitled():
    """An id with no loaded conversation resolves to the localized fallback,
    NEVER a bare id."""
    driver = '''
const pd = { tool: 'project_message', toConv: 'ghostconv0000',
             text: 'hi', outcome: 'delivered' };
process.stdout.write(_renderPeerDelivery(pd));
'''
    html = _run(_harness(extracted=_extracted(), driver=driver))
    assert 'Untitled chat' in html
    assert 'conv ghostconv' not in html


# ─────────────────────────── poisoned NC (load-bearing) ───────────────────────────

def test_nc_neutered_resolver_drops_the_title_everywhere():
    """Neuter convTitleById's lookup → both the delivery card and the queue
    source line lose the real title (fall back to 'Untitled chat'), proving the
    resolution path is load-bearing, not a tautology of the fallback."""
    # Delivery card.
    driver_card = '''
const pd = { tool: 'project_message', toConv: 'mradmzmd', text: 'x', outcome: 'delivered' };
process.stdout.write(_renderPeerDelivery(pd));
'''
    html_card = _run(_harness(extracted=_extracted(poison=True), driver=driver_card))
    assert 'Segment timeline prefill-resume' not in html_card
    assert 'Untitled chat' in html_card

    # Queue source line.
    driver_q = '''
pendingMessageQueue.set('c1', [{
  queueId: 'q1', kind: 'peer_msg', text: 'msg',
  isPeerMessage: true, fromConv: 'mradmzmd', isPeerHuman: false,
  images: [], pdfTexts: [], convRefs: [], replyQuotes: [],
}]);
renderPendingQueueUI('c1');
process.stdout.write(document.getElementById('pendingQueueBar').innerHTML);
'''
    html_q = _run(_harness(extracted=_extracted(poison=True), driver=driver_q))
    assert 'Segment timeline prefill-resume' not in html_q
    assert 'from «Untitled chat»' in html_q


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
