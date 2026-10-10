"""Pins for the Settings button language (2026-09-07).

Settings modal buttons (footer 取消/保存, system-prompt editor footer, in-body
btn-primary/btn-secondary) use the same plain `.btn .btn-primary/.btn-secondary`
markup as the project modal's Cancel/Clear/Apply row, so they must render with
the standard theme button blocks. Two Settings-only skins used to break that
equality and are retired:

- the tofu SNES pixel-bevel footer override (`[data-theme="tofu"] .settings-footer .btn-primary`)
- the all-theme raised gradient "bread" blocks (`.settings-panel[ .settings-footer] .btn-primary`)

Reintroducing a Settings-scoped primary/secondary skin makes the settings footer
diverge from the project panel buttons again; this pin fails instead.
"""

import os
import re

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# tofu block language for project controls lives in two owners: the
# stg-btn-balance gold block in 12-tofu-theme-project-controls.css, and the
# oauth action blocks that retired into settings/01-core-panels.css.
TOFU_CONTROLS_CSS = (
    os.path.join(PROJECT_ROOT, 'frontend', 'src', 'styles', 'application',
                 '12-tofu-theme-project-controls.css'),
    os.path.join(PROJECT_ROOT, 'frontend', 'src', 'styles', 'settings',
                 '01-core-panels.css'),
)


def _read(path):
    if isinstance(path, tuple):
        return '\n'.join(_read(item) for item in path)
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def test_no_settings_scoped_primary_secondary_skin_in_tofu_controls():
    css = _read(TOFU_CONTROLS_CSS)
    retired = [
        r'\.settings-panel\s+\.settings-footer\s+\.btn-primary',
        r'\.settings-panel\s+\.settings-footer\s+\.btn-secondary',
        r'\.settings-panel\s+\.btn-primary',
        r'\.settings-panel\s+\.btn-secondary',
        r'\[data-theme="tofu"\]\s+\.settings-footer\s+\.btn-primary',
        r'\[data-theme="tofu"\]\s+\.settings-footer\s+\.btn-secondary',
    ]
    for pattern in retired:
        assert not re.search(pattern, css), (
            'Settings-scoped button skin returned to '
            '12-tofu-theme-project-controls.css: ' + pattern
        )


def test_tofu_footer_resets_uppercase_legacy_of_all_theme_base():
    css = _read(TOFU_CONTROLS_CSS)
    assert re.search(
        r'\[data-theme="tofu"\]\s+\.settings-footer\s+\.btn\{[^}]*text-transform:none',
        css,
    ), 'tofu settings-footer buttons lost the text-transform:none reset'


def test_tofu_card_buttons_use_standard_block_language():
    """stg-btn-balance and the oauth btn-small family are component classes the
    global .btn-primary/.btn-secondary rules cannot reach; their tofu overrides
    must keep the same flat-fill + ink-border blocks (no gradient breads)."""
    css = _read(TOFU_CONTROLS_CSS)
    expected = {
        r'\.settings-panel\s+\.stg-btn-balance\{[^}]*background:#C9993F[^}]*'
        r'border:2px solid #1a1814': 'stg-btn-balance gold block',
        r'\.oauth-provider-actions\s+\.btn-small\.btn-danger[^}]*\{\s*background:#C0392B':
            'oauth danger red block',
        r'\.oauth-provider-actions\s+\.btn-small\.btn-primary[^}]*\{\s*background:#C9993F':
            'oauth primary gold block',
    }
    for pattern, label in expected.items():
        assert re.search(pattern, css), label + ' missing from tofu controls'
    for label, selector in (
        ('stg-btn-balance', r'\[data-theme="tofu"\]\s+\.settings-panel\s+'
         r'\.stg-btn-balance\{(?P<body>[^}]*)\}'),
        ('oauth btn-small', r'\[data-theme="tofu"\]\s+\.settings-panel\s+'
         r'\.oauth-provider-actions\s+\.btn-small[^}]*\{(?P<body>[^}]*)\}'),
    ):
        match = re.search(selector, css)
        assert match, label + ' tofu override missing'
        assert 'linear-gradient' not in match.group('body'), (
            label + ' regressed to a gradient bread.'
        )
