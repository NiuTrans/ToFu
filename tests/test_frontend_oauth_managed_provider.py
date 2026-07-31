"""tests/test_frontend_oauth_managed_provider.py — a managed subscription
(OAuth) provider card must be VISUALLY DISTINCT and self-explanatory.

WHY
---
When a user logs in with a Claude Pro/Max or ChatGPT subscription, the backend
auto-provisions a managed server_config provider (lib/oauth/outbound.py) carrying
an ``oauth`` marker + a sentinel api_key. In the general Providers list this used
to render as an unbranded gray "generic" box with a misleading "1 key" badge, and
its Delete button only spliced the card locally — leaving the OAuth token on disk
so it reappeared on the next login/refresh (the "why does it keep coming back?"
confusion).

This harness evals the REAL shipped branding.js + provider_render.js and drives
``_renderProvidersTab()`` for a managed Claude card, asserting:
  * the header icon is the REAL Claude brand logo (stg-brand-icon), NOT generic;
  * a "Subscription" badge (stg-badge-oauth) is shown instead of the "N keys" badge;
  * an explanatory note (stg-oauth-note) is present in the body;
  * the danger button is the LOGOUT affordance (_logoutManagedProvider), not the
    plain card-splice _deleteProvider;
  * NO REGRESSION: a normal cloud provider still shows the keys badge + Delete.

SOURCE-LEVEL NEUTER (mutated copy; shipped file untouched): drop the
``if (isManagedOAuth) brand = …`` remap → the card falls back to the generic box
(the reported bug reproduces), proving the remap is load-bearing.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
BRANDING = os.path.join(JS_DIR, 'settings', 'branding.js')
ICONS = os.path.join(JS_DIR, 'core', 'icons.js')
PROVIDER_RENDER = os.path.join(JS_DIR, 'settings', 'provider_render.js')

_REMAP_ARM = "if (isManagedOAuth) brand = (oauthKind === 'codex') ? 'openai' : 'claude';"


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Minimal i18n: echo keys back so labels are stable + greppable.
global.t = (k, vars) => k;
global.BASE_PATH = '';
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

// Stubs for helpers provider_render.js touches but we don't assert on.
global._localEndpointBadge = () => '<span class="stg-badge stg-ep-badge">ep</span>';
global._renderLocalEndpointsSection = () => '';
global._renderApiKeysSection = () => '';
global._guessBalanceUrl = () => '';
global._renderExtraHeadersSection = () => '';
global._findMatchingTemplate = () => null;
global._renderAccessMatrix = () => '';
global._renderModelCard = () => '<div class="stg-mcard"></div>';
global._modelPricingCache = {};
global._stgMatrixOpen = {};

// DOM shim: a single #stgProviderList element that captures innerHTML.
const _el = { innerHTML: '', querySelectorAll: () => [], getAttribute: () => null,
              classList: { contains: () => false } };
global.document = {
  getElementById: (id) => (id === 'stgProviderList' ? _el : null),
};

function loadAll(providerSrc) {
  (0, eval)(fs.readFileSync(process.argv[2], 'utf8'));  // core/icons.js (Icon())
  (0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // branding.js (_brandSvg/_detectBrand)
  (0, eval)(providerSrc);                               // provider_render.js
}

const MANAGED_CLAUDE = {
  id: 'oauth_claude', name: 'Claude (Pro/Max subscription)',
  base_url: 'https://api.anthropic.com/v1', brand: 'oauth', oauth: 'claude',
  enabled: true, api_keys: ['oauth-managed'],
  models: [{ model_id: 'claude-opus-4-5-20251101', capabilities: ['text','vision','thinking'] }],
};
const NORMAL_CLOUD = {
  id: 'user_prov', name: 'My OpenAI', base_url: 'https://api.openai.com/v1',
  enabled: true, api_keys: ['sk-abc'],
  models: [{ model_id: 'gpt-4o', capabilities: ['text'] }],
};

(async () => {
  loadAll(fs.readFileSync(process.argv[4], 'utf8'));
  if (typeof _renderProvidersTab !== 'function') {
    console.log('FAIL fn_exposed _renderProvidersTab missing'); process.exit(0);
  }
  check('fn_exposed', true);

  // ══ Managed Claude card ══
  global._stgProviders = [MANAGED_CLAUDE];
  _renderProvidersTab();
  const h = _el.innerHTML;
  // Real Claude brand logo, not the unbranded generic box.
  check('brand_icon_present', h.indexOf('stg-brand-icon') !== -1);
  // The HEADER icon (rendered at 22px via _brandSvg(brand,22)) must carry the
  // Claude amber #D97706 (see _BRAND_COLORS.claude), NOT the generic #888. We
  // match the 22px header icon specifically — model cards use an 18px icon.
  check('claude_header_color', /22px;height:22px;color:#D97706/.test(h));
  // Subscription badge shown; misleading "N keys" badge NOT shown.
  check('oauth_badge_present', h.indexOf('stg-badge-oauth') !== -1);
  check('oauth_badge_label', h.indexOf('settings.oauthManagedBadge') !== -1);
  check('no_keys_badge', h.indexOf('settings.keys') === -1);
  // Explanatory note in the body.
  check('note_present', h.indexOf('stg-oauth-note') !== -1);
  check('note_desc_key', h.indexOf('settings.oauthManagedNoteDesc') !== -1);
  // Danger button routes to logout, not plain delete.
  check('logout_button', h.indexOf('_logoutManagedProvider(0)') !== -1);
  check('logout_label', h.indexOf('settings.oauthLogoutRemove') !== -1);
  check('no_plain_delete', h.indexOf('_deleteProvider(0)') === -1);

  // ══ NO REGRESSION: normal cloud provider ══
  global._stgProviders = [NORMAL_CLOUD];
  _renderProvidersTab();
  const n = _el.innerHTML;
  check('cloud_keys_badge', n.indexOf('settings.keys') !== -1);
  check('cloud_no_oauth_badge', n.indexOf('stg-badge-oauth') === -1);
  check('cloud_no_note', n.indexOf('stg-oauth-note') === -1);
  check('cloud_plain_delete', n.indexOf('_deleteProvider(0)') !== -1);

  // ══ NEUTER: remove the brand remap ⇒ managed card falls back to generic ══
  {
    const SRC = fs.readFileSync(process.argv[4], 'utf8');
    const neutered = SRC.replace(
      "if (isManagedOAuth) brand = (oauthKind === 'codex') ? 'openai' : 'claude';",
      "if (isManagedOAuth) { /* neutered */ }");
    check('neuter_applied', neutered !== SRC);
    loadAll(neutered);
    global._stgProviders = [MANAGED_CLAUDE];
    _renderProvidersTab();
    const g = _el.innerHTML;
    // With the remap gone, the HEADER icon stays 'oauth' → generic box (#888),
    // so the 22px Claude-amber header icon disappears. (Model cards still show
    // their own 18px Claude icon — we only assert the header regressed.)
    check('neuter_no_claude_header_color', !/22px;height:22px;color:#D97706/.test(g));
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_managed_oauth_provider_card():
    src = open(PROVIDER_RENDER, encoding='utf-8').read()
    assert _REMAP_ARM in src, 'brand remap arm missing — test stale'
    assert '_logoutManagedProvider' in src, 'logout affordance missing — test stale'

    harness = os.path.join(HERE, '_oauth_managed_provider_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ICONS, BRANDING, PROVIDER_RENDER],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'managed-oauth-card failures:\n' + output
    assert output.count('PASS') >= 17, f'expected >=17 PASS lines, got:\n{output}'
