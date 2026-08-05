"""tests/test_frontend_onboarding.py — the first-run wizard.

WHY
---
A fresh Tofu install boots into a working chat UI with zero models
configured. The previous first-run behaviour opened the Settings panel on
the providers tab — a screen full of concepts (providers / presets /
matrix) that all presume the user already knows what an API key is for.
The wizard asks the ONE question a new user can answer ("API key or
subscription?") and drives the existing surfaces from there.

WHAT IS PINNED
--------------
Behavioural (drives the REAL static/js/onboarding.js in jsdom):
  1. first call renders the chooser as `.modal-overlay open` — the exact
     selector tests/conftest.py::_dismiss_onboarding_modals strips on
     keyless test servers (the 12-failure e2e class lives behind this pin);
  2. the dismissal flag suppresses re-nags, and `force` (?setup=1) bypasses it;
  3. API path: validation → probe → provider APPENDED to the live list via
     the server-config partial merge → done step + dropdown refresh;
  4. API path failure surfaces the error and persists NOTHING;
  5. OAuth path: one click closes the wizard, marks it done, and hands off
     to Settings → 订阅登录 with the login auto-kicked;
  6. skip closes + marks done.

Static ratchets:
  R1 main_toolbar_ui.js delegates to maybeShowOnboarding with
     `force: fromBootstrap` and keeps the legacy openSettings fallback
     (stale-bundle path);
  R2 every onboard.* / settings.egressGetAgent* key used by the two JS
     surfaces exists in i18n.js;
  R3 onboarding.js is registered in lib/js_bundler.py:_BUNDLE_FILES and has
     its dev-fallback <script> tag in index.html.

NEUTER: cutting the server-config update turns pin 3 red; cutting the
oauth handoff delegation turns pin 5 red.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
ONBOARD_JS = ROOT / "static" / "js" / "onboarding.js"
TOOLBAR_JS = ROOT / "static" / "js" / "main" / "main_toolbar_ui.js"
I18N_JS = ROOT / "static" / "js" / "i18n.js"
OAUTH_JS = ROOT / "static" / "js" / "settings" / "oauth.js"
INDEX_HTML = ROOT / "index.html"


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


@functools.lru_cache(maxsize=1)
def _has_jsdom() -> bool:
    try:
        subprocess.run([_node(), "-e", "require('jsdom')"], cwd=ROOT,
                       capture_output=True, check=True)
        return True
    except Exception:
        return False


HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body></body>`,
                          {{ url: 'http://localhost/' }});
    global.document = dom.window.document;
    global.window = dom.window;
    global.localStorage = dom.window.localStorage;
    global.t = (k, vars) => vars ? k + '|' + JSON.stringify(vars) : k;
    global.escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
    global.Icon = () => '';
    global.debugLog = () => {{}};
    global._rec = {{
      probeArgs: null, updatePayload: null, getCalls: 0,
      openSettings: 0, switchTab: null, oauthLogin: null, repopulate: 0,
    }};
    global.Api = {{
      providers: {{
        probe: (url, key, mp) => {{
          global._rec.probeArgs = {{ url, key, mp }};
          return Promise.resolve(global._probeResult);
        }},
      }},
      serverConfig: {{
        get: () => {{ global._rec.getCalls++;
          return Promise.resolve({{ providers: [{{ id: 'existing', api_keys: ['k0'] }}] }}); }},
        update: (payload) => {{ global._rec.updatePayload = payload;
          return Promise.resolve({{ json: () => Promise.resolve({{ ok: true }}) }}); }},
      }},
    }};
    global.openSettings = () => {{ global._rec.openSettings++; }};
    global.switchSettingsTab = (tab) => {{ global._rec.switchTab = tab; }};
    global._oauthLogin = (p) => {{ global._rec.oauthLogin = p; }};
    global._loadServerConfigAndPopulate = () => {{ global._rec.repopulate++; }};

    {shipped}

    (async () => {{
      const out = {{}};

      // (1) first call → chooser, overlay carries the e2e dismiss contract.
      out.tookOver = maybeShowOnboarding({{}});
      const m0 = document.getElementById('onboardingModal');
      out.overlayContract = !!(m0 && m0.classList.contains('modal-overlay')
                               && m0.classList.contains('open'));
      out.chooserCards = !!document.getElementById('obCardApi')
                      && !!document.getElementById('obCardOauth');
      // second call while open → no duplicate modal
      maybeShowOnboarding({{}});
      out.noDuplicate = document.querySelectorAll('#onboardingModal').length === 1;

      // (2) dismissal flag suppresses; force bypasses.
      document.getElementById('obSkip').onclick();
      out.skipClosed = !document.getElementById('onboardingModal');
      out.skipMarked = localStorage.getItem('tofu_onboarding_v1_done') === '1';
      out.suppressed = maybeShowOnboarding({{}}) === false
                    && !document.getElementById('onboardingModal');
      out.forced = maybeShowOnboarding({{ force: true }}) === true
                && !!document.getElementById('onboardingModal');

      // (3) API path — validation, then probe → append → done.
      document.getElementById('obCardApi').onclick();
      out.apiStepRendered = !!document.getElementById('obApiUrl')
                         && !!document.getElementById('obApiKey');
      // empty submit → error, no probe
      document.getElementById('obApiGo').onclick();
      await new Promise(r => setTimeout(r, 0));
      out.emptyBlocked = global._rec.probeArgs === null
        && document.getElementById('obApiStatus').style.display !== 'none';
      // filled submit
      document.getElementById('obApiUrl').value = 'api.deepseek.com';
      document.getElementById('obApiKey').value = 'sk-test';
      global._probeResult = {{ ok: true, brand: 'deepseek', name: 'DeepSeek',
        models: [{{ model_id: 'deepseek-chat' }}, {{ model_id: 'deepseek-reasoner' }}] }};
      await document.getElementById('obApiGo').onclick();
      await new Promise(r => setTimeout(r, 0));
      out.schemeNormalized = global._rec.probeArgs
        && global._rec.probeArgs.url === 'https://api.deepseek.com';
      const upd = global._rec.updatePayload;
      out.updateAppended = !!(upd && Array.isArray(upd.providers)
        && upd.providers.length === 2
        && upd.providers[0].id === 'existing'
        && upd.providers[1].base_url === 'https://api.deepseek.com'
        && upd.providers[1].api_keys[0] === 'sk-test'
        && upd.providers[1].models.length === 2
        && upd.providers[1].enabled === true);
      out.updateIsPartialMerge = !!(upd && !('presets' in upd) && !('search' in upd));
      out.doneStep = !!document.getElementById('obStart');
      out.repopulated = global._rec.repopulate === 1;
      /* NEUTER tolerance: with the persist call cut, the done step never
       * renders — that IS the guard biting, so the click must not crash
       * the harness before it can be reported. */
      if (document.getElementById('obStart')) {{
        document.getElementById('obStart').onclick();
      }}

      // (4) API path failure → error shown, NOTHING persisted.
      // NEUTER tolerance: block (3) may have ended without a done step
      // (persist cut), leaving the wizard open — close before re-entering.
      if (document.getElementById('obCloseX')) {{
        document.getElementById('obCloseX').onclick();
      }}
      maybeShowOnboarding({{ force: true }});
      if (!document.getElementById('obCardApi')) {{
        throw new Error('wizard did not reopen for block (4)');
      }}
      document.getElementById('obCardApi').onclick();
      document.getElementById('obApiUrl').value = 'https://bad.example.com';
      document.getElementById('obApiKey').value = 'sk-x';
      global._probeResult = {{ ok: false, error: 'unauthorized' }};
      global._rec.updatePayload = null;
      await document.getElementById('obApiGo').onclick();
      await new Promise(r => setTimeout(r, 0));
      out.failureShown = document.getElementById('obApiStatus')
        .textContent.includes('onboard.apiProbeFailed');
      out.nothingPersisted = global._rec.updatePayload === null;
      document.getElementById('obCloseX').onclick();

      // (5) OAuth path — one click = close + mark + handoff + auto-kick.
      maybeShowOnboarding({{ force: true }});
      document.getElementById('obCardOauth').onclick();
      out.oauthStepRendered = !!document.getElementById('obCardClaude')
                           && !!document.getElementById('obCardCodex');
      localStorage.removeItem('tofu_onboarding_v1_done');
      document.getElementById('obCardClaude').onclick();
      out.handoffClosed = !document.getElementById('onboardingModal');
      out.handoffMarked = localStorage.getItem('tofu_onboarding_v1_done') === '1';
      out.handoffOpenedSettings = global._rec.openSettings === 1;
      out.handoffTab = global._rec.switchTab;
      out.handoffLogin = global._rec.oauthLogin;

      console.log(JSON.stringify(out));
    }})().catch(e => {{ console.error(e); process.exit(1); }});
""")


def _run(neuter=None) -> dict:
    body = ONBOARD_JS.read_text(encoding="utf-8")
    if neuter:
        rewritten = neuter(body)
        assert rewritten != body, (
            "NEUTER substitution did not apply — the anchor text is gone, so "
            "this run proves nothing about whether the guard bites")
        body = rewritten
    script = HARNESS.format(shipped=body)
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr[:1500]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ══════════════════════════════════════════════════════════════════
#  Behavioural
# ══════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_first_call_renders_the_chooser_with_the_e2e_contract():
    out = _run()
    assert out["tookOver"] is True
    assert out["overlayContract"], (
        "the wizard overlay must be `.modal-overlay open` — "
        "tests/conftest.py::_dismiss_onboarding_modals strips exactly that "
        "selector on keyless test servers; any other class resurrects the "
        "12-failure click-interception class")
    assert out["chooserCards"], "the chooser must offer both paths"
    assert out["noDuplicate"], "a second entry duplicated the modal"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_dismissal_flag_suppresses_and_force_bypasses():
    out = _run()
    assert out["skipClosed"] and out["skipMarked"], (
        "skipping must close the wizard AND remember it — otherwise a "
        "keyless server re-nags on every reload")
    assert out["suppressed"], "the dismissal flag did not suppress a re-show"
    assert out["forced"], (
        "?setup=1 from bootstrap must bypass the flag — that redirect is an "
        "explicit setup intent")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_api_path_probes_appends_and_finishes():
    out = _run()
    assert out["apiStepRendered"]
    assert out["emptyBlocked"], (
        "an empty submit reached the probe — validation must block first")
    assert out["schemeNormalized"], (
        "a scheme-less Base URL was not normalized to https://")
    assert out["updateAppended"], (
        "the new provider must be APPENDED to the live providers list "
        "(existing entries preserved, probe fields carried over)")
    assert out["updateIsPartialMerge"], (
        "the wizard shipped more than {providers} — the partial merge must "
        "not touch presets/search/… it knows nothing about")
    assert out["doneStep"], "the success step never rendered"
    assert out["repopulated"], (
        "the toolbar dropdown was not refreshed — the new models stay "
        "invisible until a manual reload")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_api_path_failure_persists_nothing():
    out = _run()
    assert out["failureShown"], "a failed probe surfaced no error"
    assert out["nothingPersisted"], (
        "a FAILED probe still wrote to the server config")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_oauth_path_hands_off_to_the_existing_surface():
    out = _run()
    assert out["oauthStepRendered"]
    assert out["handoffClosed"] and out["handoffMarked"], (
        "the handoff must close the wizard and count as engagement")
    assert out["handoffOpenedSettings"], "Settings never opened"
    assert out["handoffTab"] == "oauth", (
        f"handoff landed on tab {out['handoffTab']!r}, expected 'oauth'")
    assert out["handoffLogin"] == "claude", (
        "the login was not auto-kicked — the wizard's one click must be "
        "the only click before the provider's own auth page")


# ══════════════════════════════════════════════════════════════════
#  Static ratchets
# ══════════════════════════════════════════════════════════════════

def test_R1_boot_trigger_delegates_to_the_wizard_with_fallback():
    src = TOOLBAR_JS.read_text(encoding="utf-8")
    assert "maybeShowOnboarding({ force: fromBootstrap })" in src, (
        "_maybeAutoOpenSettings no longer enters the wizard on a keyless / "
        "bootstrap boot — the wizard would never show")
    assert re.search(r"typeof maybeShowOnboarding === 'function'", src), (
        "the delegation must be typeof-guarded (stale-bundle tolerance)")
    assert "openSettings()" in src, (
        "the legacy open-Settings fallback was removed — a stale bundle "
        "would leave a fresh install with NO guidance at all")


def test_R2_every_wizard_string_is_translated():
    i18n = I18N_JS.read_text(encoding="utf-8")
    keys = set(re.findall(r"onboard\.[A-Za-z]+",
                          ONBOARD_JS.read_text(encoding="utf-8")))
    keys |= {"settings.egressGetAgent", "settings.egressGetAgentTitle",
             "settings.egressUnavailSub"}
    assert len(keys) >= 20, f"suspiciously few keys found: {sorted(keys)}"
    for key in sorted(keys):
        assert re.search(r"^\s*'%s':" % re.escape(key), i18n, re.M), (
            f"{key!r} is not defined in i18n.js — it would render as the "
            f"literal key")


def test_R3_wizard_is_bundled_and_has_its_dev_fallback_tag():
    from lib.js_bundler import _BUNDLE_FILES
    assert 'onboarding.js' in _BUNDLE_FILES, (
        "onboarding.js must be registered in lib/js_bundler.py:_BUNDLE_FILES "
        "— the production bundle would ship without the wizard")
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'static/js/onboarding.js' in html, (
        "index.html is missing the onboarding.js dev-fallback <script> tag "
        "(unbundled dev mode would 404 it)")


# ══════════════════════════════════════════════════════════════════
#  NEUTER
# ══════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_server_config_update_removed():
    """Cut the persist call → the done step must collapse AND pin 3 goes red."""
    out = _run(lambda s: s.replace(
        "    var r = await Api.serverConfig.update({ providers: providers.concat([newProv]) });\n",
        "    var r = null; /* NEUTERED */\n"))
    assert not out["updateAppended"], "NEUTER did not cut the persist"
    assert out["chooserCards"], "unrelated pins must stay green"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_oauth_handoff_delegation_removed():
    """Cut the tab switch + login kick → the handoff must go dead."""
    out = _run(lambda s: s.replace(
        "  if (typeof switchSettingsTab === 'function') switchSettingsTab('oauth');\n",
        "  /* NEUTERED */\n"))
    assert out["handoffTab"] != "oauth", "NEUTER did not cut the tab switch"
    assert out["handoffOpenedSettings"], "the Settings open must survive"


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
