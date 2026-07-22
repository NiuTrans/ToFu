"""Regression: the `data-mfp` message-fingerprint attribute must be
attribute-escaped and round-trip byte-equal through ``getAttribute()``.

BUG (reported 2026-07-22): a raw serialized tool-call string (the
``§run_command§done§…`` fingerprint tail + the ``data-mfp="…"`` attribute)
leaked into a chat bubble as visible plain text.

ROOT CAUSE: ``_msgFingerprint(msg)`` (chat_render.js) folds in
``msg._fcResolvedFp`` = ``_fcFingerprint(toolRounds)`` (finish_info.js), which
embeds a ``run_command`` round's ``title`` — the raw shell command, containing
literal ``"`` characters (e.g. ``grep -an "CacheRoundRecord…"``). The attribute
was built with a plain template literal wrapped in ``raw()``, bypassing
escaping, so the embedded ``"`` closed ``data-mfp`` early and the remainder
spilled into the DOM as text.

FIX: build the attribute with ``safeHtml`` (``"`` → ``&quot;``). Because the
surgical-diff read at chat_render.js:~636 uses ``getAttribute()`` — which
returns the browser-DECODED value — the escaped storage stays byte-equal to
``_msgFingerprint(msg)``, so the render-diff comparison is unaffected.

This test pins BOTH properties in jsdom with the real shipped
``escape_html.js`` + ``safe_html.js``.
"""

from __future__ import annotations

import os

from tests._jsdom import run_harness, JS_DIR

import pytest

pytestmark = pytest.mark.unit


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report, document } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="host"></div></body>',
  targets: [process.argv[2], process.argv[4]],  // escape_html.js, safe_html.js
});

// A fingerprint tail exactly like the real one: a run_command title carrying
// literal double-quotes (this is what broke out of the attribute).
const fp = 'assistant:abc:def::stop::fcr9|0\u00a7run_command\u00a7done\u00a7'
         + '\u00a7\u00a71\u00a7done\u00a7grep -an "CacheRoundRecord" logs/app.log~1';

// Build the attribute exactly as renderMessage does (post-fix): via safeHtml.
const attrHtml = String(safeHtml` data-mfp="${fp}"`);

// 1) The raw quote must be &quot;-escaped in the serialized HTML — it must NOT
//    appear as a bare `"` that would terminate the attribute early.
check('quote_escaped', attrHtml.indexOf('&quot;') !== -1);
check('no_bare_break', attrHtml.indexOf('logs/app.log">') === -1
                    && attrHtml.indexOf('logs/app.log"~') === -1);

// 2) Round-trip: parse the attribute into the DOM and read it back. The bug's
//    key invariant — getAttribute() must return the ORIGINAL fingerprint
//    byte-for-byte, so the surgical-diff comparison (oldFp !== newFp) stays
//    correct and does not false-positive every render.
const host = document.getElementById('host');
host.innerHTML = '<div' + attrHtml + '></div>';
const el = host.firstElementChild;
check('roundtrip_byte_equal', el.getAttribute('data-mfp') === fp);

// 3) The fingerprint tail must NOT have leaked into the element's text content
//    (the visible-garbage symptom).
check('no_text_leak', (el.textContent || '').indexOf('run_command') === -1);

report();
"""


def test_mfp_attr_escaped_and_roundtrips():
    run_harness(
        target_js=os.path.join(JS_DIR, 'core', 'escape_html.js'),
        body_js=_BODY,
        extra_targets=[os.path.join(JS_DIR, 'core', 'safe_html.js')],
        min_pass=4,
        label='data-mfp escape',
    )


def test_mfp_call_site_uses_safe_html():
    """The data-mfp attribute must be built with ``safeHtml`` (auto-escapes),
    NOT a plain template literal wrapped in ``raw()`` (bypasses escaping).

    This is the call-site companion to the jsdom behavior test above: the
    behavior test proves ``safeHtml`` escapes correctly, this proves
    renderMessage actually USES it. A regression back to ``raw(`...data-mfp=`)``
    re-opens the visible-garbage leak, so flip this red.
    """
    path = os.path.join(JS_DIR, 'ui', 'chat_render.js')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    assert 'safeHtml` data-mfp="${_msgFingerprint(msg)}"`' in src, (
        'data-mfp attribute must be built via safeHtml`...` so the fingerprint '
        '(which can embed literal `"` from a run_command title) is '
        '&quot;-escaped and cannot break out of the attribute.'
    )
    # Guard against the specific regression shape that caused the bug.
    assert 'raw(` data-mfp=' not in src, (
        'data-mfp must not be built with raw(`...`) — that bypasses escaping '
        'and lets a quote in the fingerprint spill into the DOM as text.'
    )
