"""Guards against making a terminal command part of OAuth recovery."""

from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
OAUTH_JS = Path(runtime_section_path('settings/oauth.js'))
PANEL = ROOT / 'static' / 'settings_panels' / 'oauth.html'
CSS = ROOT / 'static' / 'settings.css'


def test_terminal_command_builder_is_not_shipped_to_normal_users():
    src = OAUTH_JS.read_text(encoding='utf-8')
    for terminal_helper in (
        '_buildCurlCommand',
        '_showCurlHelper',
        '_CURL_SHELLS',
        '_curlDefaultShell',
        'oauthPasteJsonPlaceholder',
    ):
        assert terminal_helper not in src


def test_manual_fallback_only_accepts_an_authorization_result():
    html = PANEL.read_text(encoding='utf-8')
    assert 'oauthClaudeManualUrl' in html
    assert 'oauthCodexManualUrl' in html
    for implementation_detail in ('curl ', 'PowerShell', 'CMD', 'access_token'):
        assert implementation_detail not in html


def test_obsolete_shell_selector_style_is_gone():
    css = CSS.read_text(encoding='utf-8')
    assert '.oauth-curl-shell' not in css
