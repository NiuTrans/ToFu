"""Team-column NUDGE composer must stay COLLAPSED until its toggle is clicked.

Symptom (fixed 2026-07-08): every peer card in the Project Brain Team panel
showed its "send a note" composer OPEN by default — the panel looked cluttered
with a textarea + Cancel/Send box under each conversation. The JS correctly sets
``composer.hidden = true``, but the author rule
``.pb-peer-nudge-composer{display:flex}`` out-orders the UA ``[hidden]{display:none}``
(equal specificity, author wins) → the [hidden] attr had no effect and the
composer rendered anyway. jsdom tests can't catch this (jsdom resolves
``.hidden`` but never applies external stylesheets), so this is a CSS-parse
guard mirroring ``.recent-search-clear[hidden]{display:none}`` (styles.css:9744).

INVARIANT: a ``.pb-peer-nudge-composer[hidden]{display:none}`` rule exists so the
[hidden] attr wins. NEUTER removes it and asserts the guard flips.

Env-independent: parses static/styles.css directly.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _has_hidden_rule(css: str) -> bool:
    compact = re.sub(r'\s+', '', css)
    m = re.search(r'\.pb-peer-nudge-composer\[hidden\]\{([^{}]*)\}', compact)
    return bool(m and 'display:none' in m.group(1))


def test_nudge_composer_respects_hidden_attr():
    css = _read(CSS)
    assert _has_hidden_rule(css), (
        'the nudge composer has no `.pb-peer-nudge-composer[hidden]{display:none}` '
        'rule — its display:flex class rule out-orders the UA [hidden] rule, so '
        'the composer renders open under every peer card')


def test_nc_missing_hidden_rule_is_flagged():
    css = _read(CSS)
    assert _has_hidden_rule(css), 'real CSS not clean; fix before NC'
    poisoned = css.replace(
        '.pb-peer-nudge-composer[hidden]{display:none}', '', 1)
    assert poisoned != css, 'NC anchor not found — rule text drifted'
    assert not _has_hidden_rule(poisoned), (
        'the guard did NOT catch the missing [hidden] rule — not detecting the '
        'regression class.')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
