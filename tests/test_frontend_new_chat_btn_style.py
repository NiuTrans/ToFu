"""Drift-guard for the tofu New Chat button styling (2026-07-08).

What this locks in: the tofu ``.new-chat-btn`` was flat ``var(--accent)`` +
near-invisible ``--clay-sm``, so the primary sidebar CTA read as lifeless. The
optimization gave it (a) a warm-clay vertical GRADIENT (carrying the "primary"
weight, mirroring ``.folder-dialog-ok``), and (b) an enclosing shadow — but the
shadow MUST stay in tofu's neutral warm-charcoal vocabulary (the ``--clay-*``
tokens are all ``rgba(92,72,44,…)`` with no colored glow). An accent-tinted
bloom (``rgba(193,121,75,…)``) is the DARK-theme idiom and reads as "off"
against tofu's calm paper aesthetic + the sibling send-btn (flat accent +
``--clay-sm``).

Guards, for the idle rule ``[data-theme="tofu"] .new-chat-btn``:
  (a) background is a ``linear-gradient`` (primary weight kept);
  (b) box-shadow carries NO accent-colored glow — no ``rgba(193,121,75,…)``
      and, more generally, no clay-accent RGB triple — so the dark-theme glow
      idiom can never creep back;
  (c) NEUTER: re-inject the accent glow on a COPY → assertion (b) must flip
      False (proves the guard is load-bearing).

Env-independent: parses static/styles.css directly (no node/jsdom), using the
same _strip_comments + brace-split rule-splitter as the other styles.css
drift-guards (comments can hold literal braces — the documented test trap).
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')

SELECTOR = '[data-theme="tofu"] .new-chat-btn'
# The clay accent RGB triple — its presence in a box-shadow is the dark-theme
# colored-glow idiom that must NOT appear in the tofu button's shadow.
ACCENT_GLOW = 'rgba(193,121,75'


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _strip_comments(css: str) -> str:
    """Remove /* … */ comments (they can contain literal braces that corrupt the
    naive brace-based rule splitter — the documented styles.css test trap)."""
    return re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)


def _rule_body(css: str, selector: str) -> str | None:
    """Return the declaration body of the FIRST rule whose selector-list is
    EXACTLY `selector` (whitespace-normalized). Innermost rules only."""
    css = _strip_comments(css)
    want = re.sub(r'\s+', ' ', selector).strip()
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        sel = re.sub(r'\s+', ' ', m.group(1)).strip()
        if sel == want:
            return m.group(2)
    return None


def _box_shadow(body: str) -> str | None:
    """Extract the `box-shadow:<value>` declaration value from a rule body."""
    m = re.search(r'box-shadow\s*:\s*([^;]+)', body)
    return m.group(1).strip() if m else None


def _has_accent_glow(shadow: str) -> bool:
    """True if the box-shadow value carries the clay-accent RGB triple (the
    dark-theme colored-glow idiom)."""
    return ACCENT_GLOW in shadow.replace(' ', '')


def test_new_chat_btn_has_gradient_background():
    """The tofu New Chat button keeps its warm-clay gradient (primary weight)."""
    body = _rule_body(_read(CSS), SELECTOR)
    assert body is not None, (
        f'{SELECTOR} rule not found (structure changed?)')
    m = re.search(r'background\s*:\s*([^;]+)', body)
    assert m is not None, f'{SELECTOR} has no background declaration.\nbody=' + body
    assert 'linear-gradient' in m.group(1), (
        f'{SELECTOR} background is no longer a gradient — the primary-weight '
        'gradient was reverted to a flat fill.\nbackground=' + m.group(1))


def test_new_chat_btn_shadow_has_no_accent_glow():
    """The idle box-shadow must stay in tofu's neutral warm-charcoal vocabulary —
    NO accent-colored glow (the dark-theme idiom)."""
    body = _rule_body(_read(CSS), SELECTOR)
    assert body is not None, f'{SELECTOR} rule not found'
    shadow = _box_shadow(body)
    assert shadow is not None, f'{SELECTOR} has no box-shadow.\nbody=' + body
    assert not _has_accent_glow(shadow), (
        f'{SELECTOR} box-shadow re-introduced the accent-colored glow '
        f'({ACCENT_GLOW}…) — that is the dark-theme idiom and reads as "off" '
        'against tofu\'s neutral clay shadows. Use rgba(92,72,44,…) instead.\n'
        'box-shadow=' + shadow)


def test_nc_accent_glow_is_flagged():
    """NEUTER: re-inject the accent glow into the shadow on a COPY → the
    no-accent-glow assertion must catch it (guard is load-bearing)."""
    body = _rule_body(_read(CSS), SELECTOR)
    assert body is not None
    shadow = _box_shadow(body)
    assert shadow is not None and not _has_accent_glow(shadow), 'fix real CSS first'
    poisoned = shadow.replace('rgba(92,72,44,0.10)', 'rgba(193,121,75,0.22)', 1)
    assert poisoned != shadow, 'neuter did not rewrite the shadow — test is stale'
    assert _has_accent_glow(poisoned), (
        'the no-accent-glow assertion would NOT flag a re-introduced accent '
        'bloom — guard is not load-bearing.')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
