"""tests/test_chat_mode_parity.py — FE↔BE chat-mode table parity.

The three-tier dial has TWO derivation tables that MUST stay byte-equal:

  * Backend: ``lib/tasks_pkg/chat_mode.chat_mode_defaults`` (authoritative).
  * Frontend: ``_CHAT_MODE_DEFAULTS`` in
    ``static/js/main/main_toolbar_ui.js`` (mirror the user sees).

If they drift, the UI dial and the resolved tool set silently disagree — the
exact class of FE/BE desync the owner asked to prevent. This test parses the
JS object literal and asserts it equals the Python dict for every tier.

Parsing note: the JS table is a plain object literal with JS boolean literals
(true/false) and single/undecorated keys — we normalise it to JSON and load
it, rather than importing a JS engine.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg.chat_mode import CHAT_MODES, chat_mode_defaults

_JS = Path(__file__).resolve().parent.parent / 'static' / 'js' / 'main' / 'main_toolbar_ui.js'


def _extract_js_table() -> dict:
    """Parse the ``const _CHAT_MODE_DEFAULTS = { ... };`` literal into a dict."""
    src = _JS.read_text(encoding='utf-8')
    m = re.search(r'const\s+_CHAT_MODE_DEFAULTS\s*=\s*\{', src)
    assert m, '_CHAT_MODE_DEFAULTS literal not found in main_toolbar_ui.js'
    # Walk braces from the opening { to its match.
    start = m.end() - 1
    depth = 0
    end = None
    for i in range(start, len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end is not None, 'unbalanced braces in _CHAT_MODE_DEFAULTS'
    body = src[start:end]
    # Strip line comments.
    body = re.sub(r'//[^\n]*', '', body)
    # Single-quoted string VALUES → double-quoted (JS uses 'multi').
    body = re.sub(r"'([^']*)'", r'"\1"', body)
    # Quote bare object keys (air:, searchMode:, …).
    body = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', body)
    # Drop trailing commas before } or ].
    body = re.sub(r',(\s*[}\]])', r'\1', body)
    return json.loads(body)


class TestChatModeParity(unittest.TestCase):
    def test_frontend_table_equals_backend(self):
        js_table = _extract_js_table()
        # Same set of tiers.
        self.assertEqual(set(js_table.keys()), set(CHAT_MODES),
                         'FE and BE must define the same tiers')
        # Each tier's pinned-flag dict must be byte-equal.
        for mode in CHAT_MODES:
            self.assertEqual(js_table[mode], chat_mode_defaults(mode),
                             f'tier {mode!r} FE/BE table drift')


if __name__ == '__main__':
    unittest.main(verbosity=2)
