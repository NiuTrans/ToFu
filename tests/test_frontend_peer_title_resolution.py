#!/usr/bin/env python3
"""Peer delivery surfaces render a conversation TITLE, never a raw id.

WHY
---
The Project-Brain peer surfaces carried a bare conversation id where a human
expects a title:

  • the ``project_message`` / ``project_intervene`` delivery card showed
    ``conv mradmzmd`` (an 8-char display id — meaningless to a user).

The fix routes every id→title through the typed catalog query in
``conversation/application/conversation-catalog-queries.ts``: match the full
id, then a UNIQUE prefix
(so an 8-char display id still resolves against the loaded ``conversations``
list), else fall back to a localized "Untitled chat" — NEVER a bare id. The
delivery card (``_renderPeerDelivery`` in ``ui/tool_rounds_rich.js``) calls it.

The queued peer/operator SOURCE LINE is gone: queued messages now render inline
in the transcript via ``renderNativeQueueItem``
(``conversation/ui/classic-conversation-renderers.ts``), which does not resolve
a conversation title. Those queue-source-line assertions were removed when the
input-bar queue was deleted in the Vite + storage runtime migration (commit
``026042a2``); the retained title-resolution consumer is the delivery card.

This test bundles the real typed query and extracts the retained
delivery-card consumer, then evaluates it with a real ``conversations`` list,
asserting the resolved TITLE appears (not the id) and the id is demoted to the
``title=`` tooltip. It closes the coverage gap where
``test_frontend_brain_tool_render.py`` only passed its ``peermsg_target`` check
because ``convTitleById`` was UNDEFINED in that harness (the resolution path
was never executed).

Poisoned NC: inject an empty catalog into the lexical wrapper so it always
returns the fallback label → the delivery card stops showing the real title,
proving the resolution is load-bearing (not a tautology of the fallback).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest
from tests._runtime_sections import (
    native_module_path,
    orchestration_legacy_test_root as _legacy_test_root,
)

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
# The title policy is a native module; retained consumers are still resolved by
# symbol so further section splits do not turn into false product regressions.
QUERY_OWNER = os.path.join(
    ROOT,
    'frontend', 'src', 'conversation', 'application',
    'conversation-catalog-queries.ts',
)
QUERY_BUNDLE = native_module_path(
    '.native/peer-title-query-contract.js', QUERY_OWNER,
)


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
    """The real typed title query + the retained delivery-card consumer.

    The queue source line (``renderPendingQueueUI`` peer/operator attribution)
    no longer lives in ``main_send_pipeline.js`` — queued messages render
    inline in the transcript. Its delivery-card sibling still resolves titles
    through the typed query and is exercised here.
    """
    query_bundle = _read(QUERY_BUNDLE)
    catalog_expression = '[]' if poison else 'conversations'
    fn_title = f'''function convTitleById(conversationId) {{
  return conversationTitleById(
    {catalog_expression}, conversationId, t('toast.untitledConv'));
}}'''
    fn_delivery = _extract_fn(
        _read(_src_defining('_renderPeerDelivery')), '_renderPeerDelivery')
    return '\n'.join([query_bundle, fn_title, fn_delivery])


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
    """Neuter convTitleById's lookup → the delivery card loses the real title
    (falls back to 'Untitled chat'), proving the resolution path is load-bearing,
    not a tautology of the fallback. (The queue source line moved inline into
    the transcript renderer and is no longer driven through this section.)"""
    driver_card = '''
const pd = { tool: 'project_message', toConv: 'mradmzmd', text: 'x', outcome: 'delivered' };
process.stdout.write(_renderPeerDelivery(pd));
'''
    html_card = _run(_harness(extracted=_extracted(poison=True), driver=driver_card))
    assert 'Segment timeline prefill-resume' not in html_card
    assert 'Untitled chat' in html_card


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
