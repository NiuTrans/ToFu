"""Primary chat action takes its surface and ink from the theme.

The negative control poisons an in-memory copy, never shipped styles.
Actual contrast, focus and responsive geometry are covered by timeline browser
captures in all themes, including Tofu's restrained accent tint.
"""
from pathlib import Path
import re
import pytest
from tests._source_scan import strip_comments

pytestmark = pytest.mark.unit
CSS = Path(__file__).resolve().parents[2] / 'static/styles.css'


def _primary_body(css):
    clean = strip_comments(css, lang='css', inline=True)
    return next(m.group(2) for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', clean)
                if m.group(1).strip() == '.new-chat-btn')


def _themeable_primary(body):
    return all(token in body for token in (
        'background:var(--chat-primary-background)',
        'color:var(--chat-primary-ink)',
        'box-shadow:var(--chat-primary-shadow)'))


def test_new_chat_action_preserves_theme_identity():
    assert _themeable_primary(_primary_body(CSS.read_text()))


def test_nc_hardcoded_primary_surface_is_flagged():
    body = _primary_body(CSS.read_text())
    assert _themeable_primary(body)
    poisoned = body.replace('background:var(--chat-primary-background)', 'background:black', 1)
    assert poisoned != body and not _themeable_primary(poisoned)
