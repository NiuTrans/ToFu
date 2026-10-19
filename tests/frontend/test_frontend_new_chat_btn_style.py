"""Primary chat action uses shared ink/paper, without texture or colored glow.

The negative control poisons an in-memory copy, never shipped styles.
Actual contrast, focus and responsive geometry are covered by timeline browser
captures in all themes. The old baked-gradient contract is retired.
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


def _quiet_primary(body):
    return ('gradient(' not in body and 'url(' not in body
            and re.search(r'box-shadow:\s*none\s*;', body) is not None)


def test_new_chat_action_uses_shared_contrasting_surface():
    body = _primary_body(CSS.read_text())
    assert 'background:var(--text-primary)' in body
    assert 'color:var(--bg-primary)' in body
    assert _quiet_primary(body)


def test_nc_decorative_glow_is_flagged():
    body = _primary_body(CSS.read_text())
    assert _quiet_primary(body)
    poisoned = body.replace('box-shadow:none', 'box-shadow:0 0 20px rgba(193,121,75,.4)', 1)
    assert poisoned != body and not _quiet_primary(poisoned)
    assert not _quiet_primary(body.replace('background:var(--text-primary)',
        'background:linear-gradient(red,blue)', 1))
