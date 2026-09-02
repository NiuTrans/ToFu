"""Guards against assigning network-agent setup to subscription users."""

from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
OAUTH_JS = Path(runtime_section_path('settings/oauth.js'))
PANEL = ROOT / 'static' / 'settings_panels' / 'oauth.html'


def _egress_renderer() -> str:
    src = OAUTH_JS.read_text(encoding='utf-8')
    start = src.index('function _renderEgressLine(provider, egress)')
    end = src.index('function _updateOAuthCard(provider, status)', start)
    return src[start:end]


def test_egress_renderer_never_offers_an_installer_or_permission_work():
    renderer = _egress_renderer()
    for burden in (
        'openLocalControlModal',
        'EgressAgentBtn',
        'settings.egressGetAgent',
        'setInterval',
    ):
        assert burden not in renderer


def test_subscription_markup_has_no_agent_setup_control():
    html = PANEL.read_text(encoding='utf-8')
    for burden in ('EgressAgentBtn', '安装受控端', '订阅转发', 'allow-egress'):
        assert burden not in html
