"""Pins for the compact subscription-login (oauth) settings panel (2026-09-07).

Owner direction: the 订阅登录 tab was too sparse — provider action buttons and
the email/egress status strips each burned a whole line. The compact layout
contract is:

- `.oauth-provider-actions` lives INSIDE `.oauth-provider-header` so buttons
  share the title/badge row instead of a line of their own;
- `.oauth-provider-info` (email) and `.oauth-egress-line` share one
  `.oauth-meta-row` flex line per card;
- the meta row collapses cleanly when every child is display:none
  (`.oauth-meta-row:not(:empty)` owns the margin, children own none).

The JS renderers (runtime/sections/settings/oauth.js) toggle these elements by
id with style.display ''/'none', which composes with the flex meta row only if
the children carry no outer margins of their own.
"""

import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OAUTH_HTML = os.path.join(PROJECT_ROOT, 'static', 'settings_panels', 'oauth.html')
SETTINGS_CSS = os.path.join(
    PROJECT_ROOT, 'frontend', 'src', 'styles', 'settings', '01-core-panels.css')


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _card(html, card_id):
    start = html.index('id="' + card_id + '"')
    header_end = html.index('</div>', html.index('oauth-provider-actions', start))
    return html[start:header_end]


def test_actions_live_inside_provider_header_both_cards():
    html = _read(OAUTH_HTML)
    for card_id in ('oauthClaudeCard', 'oauthCodexCard'):
        card = _card(html, card_id)
        header = card[card.index('oauth-provider-header'):]
        assert 'oauth-provider-actions' in header, (
            card_id + ' action buttons fell out of the header row — '
            'they take a whole line again.'
        )


def test_email_and_egress_share_one_meta_row_both_cards():
    html = _read(OAUTH_HTML)
    for provider in ('Claude', 'Codex'):
        row = re.search(
            r'<div class="oauth-meta-row">(?P<body>.*?)</div>\s*</div>',
            html, re.S)
        assert row, 'oauth-meta-row wrapper missing from oauth.html'
        assert 'id="oauth' + provider + 'Info"' in html
        meta = re.search(
            r'<div class="oauth-meta-row">(?P<body>.*?)'
            r'<div class="oauth-egress-line" id="oauth' + provider + 'Egress"',
            html, re.S)
        assert meta, provider + ' egress line is not inside the meta row'
        assert 'id="oauth' + provider + 'Info"' in meta.group('body'), (
            provider + ' email info is not inside the meta row'
        )


def test_meta_row_collapses_when_all_children_hidden():
    css = _read(SETTINGS_CSS)
    assert re.search(r'\.oauth-meta-row:not\(:empty\)\s*\{\s*margin-bottom', css), (
        'meta-row margin must live on :not(:empty) so an all-hidden row '
        'leaves no stray gap.'
    )
    info = re.search(r'\.oauth-provider-info\s*\{(?P<body>[^}]*)\}', css)
    egress = re.search(r'\.oauth-egress-line\s*\{(?P<body>[^}]*)\}', css)
    assert info and egress
    assert 'margin: 0;' in info.group('body'), 'email strip regained an outer margin'
    assert re.search(r'\n\s*margin: 0;', egress.group('body')), (
        'egress line regained an outer margin'
    )
