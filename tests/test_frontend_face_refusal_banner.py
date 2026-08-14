#!/usr/bin/env python3
"""tests/test_frontend_face_refusal_banner.py — a refusal the user can SEE.

WHY
===
``dispatcher.face_refusals`` records every model entry that was REFUSED at
slot-build time (a Claude model on a dual-face gateway whose provider declares
no ``faces.anthropic``). Recording it is only half the job: without a visible
banner the model is simply ABSENT from the picker with no explanation — which
is the exact silent-failure class the fail-loud refusal exists to prevent. A
refusal the user cannot see is its own silent failure.

WHAT IS GUARDED (results, not implementation)
---------------------------------------------
  * The refusal reaches the frontend: ``/api/v1/server-config`` carries a
    ``face_refusals`` key.
  * A provider WITH refusals renders the banner, naming the refused models.
  * A provider WITHOUT refusals renders NO banner (no permanent scare text).
  * Refusals are matched to the RIGHT provider card, never smeared across all.
  * ★ The CSS actually paints it. A class that flips while the stylesheet
    ignores it is the "guard goes green, user sees nothing" shape this project
    keeps hitting — so the banner's styles are asserted to exist in the SHIPPED
    stylesheet, not just the class name in the markup.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_frontend_face_refusal_banner.py -v
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RENDER = runtime_section_path('settings/provider_render.js')
_CSS = os.path.join(_ROOT, 'static', 'styles.css')

_HARNESS = r'''
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

global.window = global;
const mkEl = () => ({
  innerHTML: '', style: {}, classList: { add(){}, contains: () => false, toggle(){} },
  appendChild(){}, setAttribute(){}, getAttribute: () => null,
  querySelector: () => null, querySelectorAll: () => [],
});
global.document = {
  getElementById: (id) => (id === 'stgProviderList' ? LIST : null),
  querySelector: () => null, querySelectorAll: () => [],
  createElement: mkEl, addEventListener(){},
};
const LIST = mkEl();

global.t = (k, p) => (p && p.n !== undefined ? k + ':' + p.n : k);
global.escapeHtml = (s) => String(s == null ? '' : s);
global.Icon = () => '';
global._brandSvg = () => '';
global._detectBrand = () => 'yourprovider';
global._modelPricingCache = {};
global.isChatModel = () => true;
global._fitMatrixPanelWidth = () => {};
global._renderModelCard = () => '<div class="stg-mcard"></div>';
global._localEndpointBadge = () => '';
global._findMatchingTemplate = () => null;
global._loadExternalProviderTemplates = async () => {};
global._PROVIDER_TEMPLATES = [];
global._stgMatrixProbe = {};
global._stgMatrixOpen = {};
global._modelHealth = {};
global._matrixKeys = (p) => (p.api_keys || []);
global._providerId = (i) => 'p' + i;

const CASE = JSON.parse(process.argv[3]);
global._stgProviders = CASE.providers;
global._stgFaceRefusals = CASE.refusals;

(0, eval)(src);
_renderProvidersTab();
console.log(JSON.stringify({ html: LIST.innerHTML }));
'''


def _render(providers, refusals):
    import shutil
    if not shutil.which('node'):
        pytest.skip('node not available')
    harness = os.path.join('/tmp', 'face_refusal_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    payload = json.dumps({'providers': providers, 'refusals': refusals})
    out = subprocess.run([shutil.which('node'), harness, _RENDER, payload],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2500:]
    return json.loads(out.stdout.strip().splitlines()[-1])['html']


_MERGED = {
    'id': 'default', 'name': 'Default',
    'base_url': 'https://api.openai.com/v1',
    'api_keys': ['k1'], 'enabled': True,
    'models': [{'model_id': 'kimi-k3', 'capabilities': ['text']}],
}
_OTHER = {
    'id': 'openai', 'name': 'OpenAI',
    'base_url': 'https://api.openai.com/v1',
    'api_keys': ['k1'], 'enabled': True,
    'models': [{'model_id': 'gpt-4.1-mini', 'capabilities': ['text']}],
}
# provider_id is derived from the card, not spelled out: export.py rewrites
# the card's id, and a refusal only renders when it names the SAME provider.
_REFUSALS = [
    {'provider_id': _MERGED['id'], 'model_id': 'claude-opus-5',
     'error': 'declares no anthropic face'},
    {'provider_id': _MERGED['id'], 'model_id': 'claude-fable-5',
     'error': 'declares no anthropic face'},
]


# ═══════════════════════════════════════════════════════════

def test_server_config_exposes_face_refusals():
    """The backend must actually SEND the refusals — without this key the
    frontend has nothing to render and the banner is dead code."""
    import ast
    src = open(os.path.join(_ROOT, 'routes', 'config.py'), encoding='utf-8').read()
    assert "'face_refusals'" in src, (
        'routes/config.py must expose face_refusals in the server-config payload')
    ast.parse(src)


def test_banner_renders_and_names_the_refused_models():
    html = _render([_MERGED], _REFUSALS)
    assert 'stg-face-refusal' in html, (
        'no refusal banner rendered — the refused models would be silently '
        'missing from the picker with no explanation')
    assert 'claude-opus-5' in html
    assert 'claude-fable-5' in html


def test_no_banner_when_nothing_was_refused():
    """A permanent scare banner would train the user to ignore it."""
    html = _render([_MERGED], [])
    assert 'stg-face-refusal' not in html


def test_refusals_are_scoped_to_their_own_provider_card():
    """A refusal on the gateway card must not smear onto the OpenAI card."""
    html = _render([_OTHER], _REFUSALS)
    assert 'stg-face-refusal' not in html, (
        'refusals leaked onto an unrelated provider card')


def test_banner_carries_the_actionable_hint():
    html = _render([_MERGED], _REFUSALS)
    assert 'faceRefusedHint' in html or 'settings.faceRefusedHint' in html, (
        'the banner must tell the user HOW to fix it (sync from template)')


def test_i18n_keys_are_defined_in_both_languages():
    """A banner that renders the raw key name is the same defect as no banner."""
    locale_dir = os.path.join(_ROOT, 'frontend', 'src', 'i18n', 'locales')
    locales = [
        json.loads(open(os.path.join(locale_dir, f'{lang}.json'),
                        encoding='utf-8').read())
        for lang in ('zh', 'en')
    ]
    for key in ('settings.faceRefusedTitle', 'settings.faceRefusedHint'):
        assert all(key in locale and locale[key] for locale in locales), \
            '%s missing zh or en' % key


def test_the_stylesheet_actually_paints_the_banner():
    """★ The class flipping is worthless if the shipped CSS ignores it.

    This is the "guard green / user sees nothing" shape: asserting only the
    markup would pass even with zero styles, leaving an unstyled bare line the
    user reads as noise rather than a warning.
    """
    css = open(_CSS, encoding='utf-8').read()
    for sel in ('.stg-face-refusal', '.stg-face-refusal-title',
                '.stg-face-refusal-models', '.stg-face-refusal-hint'):
        assert sel in css, '%s has no styles in the shipped stylesheet' % sel
    block = css[css.index('.stg-face-refusal{'):]
    block = block[:block.index('}')]
    assert 'border' in block and 'background' in block, (
        'the banner must be visually distinct (border + background), not a '
        'bare line of text: %r' % block)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
