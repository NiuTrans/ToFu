"""Guards for the user-facing subscription-login boundary."""

import json
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / 'static' / 'settings_panels' / 'oauth.html'
OAUTH_JS = Path(runtime_section_path('settings/oauth.js'))
LOCALE_DIR = ROOT / 'frontend/src/i18n/locales'


def test_primary_surface_only_asks_for_official_authorization():
    html = PANEL.read_text(encoding='utf-8')
    assert 'settings.oauthUserRole' in html
    assert 'settings.oauthSystemRole' in html
    assert 'settings.oauthProviderClaude' in html
    assert 'settings.oauthProviderCodex' in html
    for implementation_detail in (
        'oauthDirectHeading',
        'settings.oauthDirectTitle',
        'settings.oauthDirectClaude',
        'settings.oauthDirectCodex',
        'CLIProxyAPI',
        '订阅适配器',
        '服务器直连',
        '受控端',
    ):
        assert implementation_detail not in html


def test_primary_surface_does_not_preemptively_assign_manual_recovery():
    html = PANEL.read_text(encoding='utf-8')
    assert 'oauthClaudeManual' in html
    assert 'oauthCodexManual' in html
    assert 'style="display:none"' in html
    for burden in ('Clash/VPN', '本机终端', 'curl ', 'access_token'):
        assert burden not in html


def test_subscription_page_never_starts_or_polls_the_legacy_adapter():
    src = OAUTH_JS.read_text(encoding='utf-8')
    assert '_adapterStartPolling()' not in src
    assert "Api.get('/api/v1/adapter/status'" not in src
    assert 'settings.adapterTitle' not in src


def test_egress_route_is_rendered_without_assigning_setup_work():
    src = OAUTH_JS.read_text(encoding='utf-8')
    assert 'function _renderEgressLine(provider, egress)' in src
    renderer = src[src.index(
        'function _renderEgressLine(provider, egress)'):src.index(
        'function _updateOAuthCard(provider, status)')]
    assert "el.style.display = '';" in renderer
    for route_key in (
        'settings.egressChecking',
        'settings.egressDirect',
        'settings.egressViaProxy',
        'settings.egressViaAgent',
        'settings.egressAgentNoCap',
        'settings.egressUnavailable',
    ):
        assert route_key in renderer
    for setup_work in ('openLocalControlModal', 'settings.egressGetAgent'):
        assert setup_work not in renderer


def test_user_facing_copy_states_the_ownership_boundary():
    locales = {lang: json.loads((LOCALE_DIR / f'{lang}.json').read_text())
               for lang in ('zh', 'en')}
    for key in (
        'settings.oauthUserRole',
        'settings.oauthSystemRole',
        'settings.oauthAutomaticRecovery',
        'settings.oauthProviderClaude',
        'settings.oauthProviderCodex',
    ):
        assert all(locales[lang].get(key) for lang in locales)
    assert locales['zh']['settings.oauthUserRole'] == '你只需在官方页面完成登录和授权'
    assert locales['zh']['settings.oauthSystemRole'] == \
        '网络选择、凭据交换、模型接入与后续刷新均由 Tofu 自动完成'


def test_terminal_command_is_not_a_user_recovery_path():
    src = OAUTH_JS.read_text(encoding='utf-8')
    assert '_showCurlHelper' not in src
    assert 'oauthPasteJsonPlaceholder' not in src
    assert '_CURL_SHELLS' not in src
