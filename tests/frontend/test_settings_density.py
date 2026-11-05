"""Settings density decisions (docs/DESIGN_SYSTEM.md, "Settings density and copy").

Owner direction (2026-11-01): the General tab looked empty because its
everyday switches were folded into disclosures, and several tabs carried
explainer decoration that controlled nothing. These pins keep the audited
structure from drifting back; the full review is
designs/ui-gallery/settings_audit.py.
"""
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
PANELS = ROOT / 'static' / 'settings_panels'
GENERAL_SWITCHES = ('settingAutoGenerateTitle', 'settingDebugMode', 'settingOptimizerEnabled',
                    'settingTradingEnabled', 'settingPptxTranslateEnabled')


class _Ancestry(HTMLParser):
    """Record each id's open-element ancestry (tag, class) in one pass."""

    VOID = {'input', 'br', 'img', 'hr', 'meta', 'link', 'path', 'rect', 'circle',
            'line', 'polyline', 'ellipse', 'g'}

    def __init__(self):
        super().__init__()
        self.stack, self.ancestry, self.classes = [], {}, set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = tuple((attrs.get('class') or '').split())
        self.classes.update(classes)
        if attrs.get('id'):
            self.ancestry[attrs['id']] = list(self.stack)
        if tag not in self.VOID:
            self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()


def _parse(name):
    parser = _Ancestry()
    parser.feed((PANELS / name).read_text(encoding='utf-8'))
    return parser


def test_general_switches_are_visible_in_one_list():
    general = _parse('general.html')
    for control in GENERAL_SWITCHES:
        ancestry = general.ancestry[control]
        assert not any(tag == 'details' for tag, _ in ancestry), (
            f'#{control} is folded into a disclosure again; General then opens '
            'nearly empty')
        assert any('settings-list' in classes for _, classes in ancestry), control


def test_system_prompt_is_one_row_with_its_live_summary():
    general = _parse('general.html')
    for control in ('settingSystem', 'settingSystemSummary'):
        assert any('sysprompt-row' in classes for _, classes in general.ancestry[control]), control


@pytest.mark.parametrize('panel,decoration', [
    ('oauth.html', 'oauth-steps'),
    ('oauth.html', 'oauth-responsibility'),
    ('advanced.html', 'tool-discovery-card'),
    ('advanced.html', 'tool-discovery-flow'),
    ('preferences.html', 'ctx-eyebrow'),
])
def test_explainer_decoration_stays_out(panel, decoration):
    assert decoration not in _parse(panel).classes


def test_checkboxes_inside_fields_keep_their_native_box():
    """Field and modal input rules size inputs like text boxes; choices opt out."""
    settings = (ROOT / 'frontend/src/styles/settings/04-search-network-advanced.css').read_text(
        encoding='utf-8')
    rule = settings.index(
        '.settings-panel .settings-field :is(input[type="checkbox"], input[type="radio"]) {')
    body = settings[rule:settings.index('}', rule)]
    for declaration in ('width: auto;', 'height: auto;', 'margin: 0;'):
        assert declaration in body
