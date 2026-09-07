"""Guards that the legacy adapter console stays out of subscription login."""

from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
OAUTH_JS = Path(runtime_section_path('settings/oauth.js'))
PANEL = ROOT / 'static' / 'settings_panels' / 'oauth.html'
CSS = ROOT / 'static' / 'settings.css'


def test_adapter_lifecycle_is_not_part_of_the_normal_oauth_runtime():
    src = OAUTH_JS.read_text(encoding='utf-8')
    for legacy_surface in (
        '_adapterEnsureCard',
        '_renderAdapterRows',
        '_adapterStartPolling',
        '/api/v1/adapter/',
        'settings.adapterTitle',
    ):
        assert legacy_surface not in src


def test_adapter_controls_are_not_rendered_on_the_subscription_page():
    html = PANEL.read_text(encoding='utf-8')
    for legacy_surface in (
        'adapterCard',
        'adapterRows',
        'adapter-start-btn',
        'adapter-stop-btn',
        'CLIProxyAPI',
        '订阅适配器',
    ):
        assert legacy_surface not in html


def test_adapter_console_styles_are_not_shipped_with_the_normal_page():
    css = CSS.read_text(encoding='utf-8')
    for selector in (
        '.adapter-empty',
        '.adapter-agent-row',
        '.adapter-progress',
        '.adapter-account-list',
        '.adapter-error-line',
    ):
        assert selector not in css
