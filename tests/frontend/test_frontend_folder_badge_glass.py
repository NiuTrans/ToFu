"""Current folder badges use the shared quiet presentation, not a retired chip."""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
CSS = REPO / 'frontend/src/styles/application/11-tofu-theme-foundation.css'
BADGES = REPO / 'frontend/src/conversation/ui/new-chat-folder-badges.ts'


def _rule_body(selector: str) -> str:
    css = CSS.read_text(encoding='utf-8')
    match = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
    assert match, f'missing current badge rule: {selector}'
    return match.group(1)


def test_current_badges_have_one_quiet_presentation():
    assert 'className = \'topbar-folder-badge\'' in BADGES.read_text()
    assert 'className = \'welcome-folder-badge\'' in BADGES.read_text()
    for selector in ('[data-theme="tofu"] .topbar-folder-badge',
                     '[data-theme="tofu"] .welcome-folder-badge'):
        body = _rule_body(selector)
        assert 'var(--text-secondary)' in body
        assert 'var(--sans-body)' in body
        assert 'border-top-color' not in body
        assert not re.search(r'box-shadow:[^;]*\b0 2px 0 ', body)


def test_neuter_restoring_bevel_is_detected():
    body = _rule_body('[data-theme="tofu"] .topbar-folder-badge')
    neutered = body + 'border-top-color:red;box-shadow:0 2px 0 black;'
    assert 'border-top-color' in neutered
    assert re.search(r'box-shadow:[^;]*\b0 2px 0 ', neutered)
