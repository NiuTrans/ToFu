#!/usr/bin/env python3
"""Frontend test — the preset-tab "测试全部" probe-health dots.

WHY
---
The user asked to "test that each model is in a normal state so I can select
with confidence." The preset tab now shows a health dot per model row, fed
by the EXISTING probe-cells engine and folded with the SHARED pool judgment
(core/model_health.js). Critically, the verdict must carry its SOURCE: the
two Meituan faces share API keys but probe DIFFERENT protocols, so a green
dot on a Claude model must say "this came from the Anthropic-native face",
not be silently attributed to the compatible face.

WHAT IS GUARDED (results, not implementation)
------------------------------------------------------------------
  * Each dropdown row renders a health dot.
  * "测试全部" sends ONE probeCellsStart per enabled provider, each with its
    OWN protocol and oauth marker.
  * After probing, a model whose provider returned ok paints a usable dot;
    a fully-failing model paints 'down'; an unprobed model stays muted.
  * The dot tooltip carries the PROVIDER NAME and PROTOCOL of the source
    cells (the attribution the user needs to trust a merged display).
  * A stale snapshot (>24h) paints 'stale', never green.

NEUTER: drop the provider/protocol attribution from the tooltip → a merged
green dot can no longer be traced to its source face (red).
"""

from __future__ import annotations

import json
import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

VISIBILITY_JS = os.path.join(JS_DIR, 'settings', 'visibility_defaults.js')
MODEL_HEALTH_JS = os.path.join(JS_DIR, 'core', 'model_health.js')
MODEL_GROUP_JS = os.path.join(JS_DIR, 'core', 'model_group.js')

_HTML = ('<!DOCTYPE html><body>'
         '<div id="stgDropdownVisibility"></div>'
         '<button id="stgProbeAllModelsBtn"></button></body>')

_BODY = r'''
const fs = require('fs');
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, window, check, report } = setup({
  root: process.argv[3],
  html: HTML_PLACEHOLDER,
  targets: [process.argv[2], process.argv[4], process.argv[5]],
  globals: {
    _detectBrand: (s) => /sankuai|meituan|longcat|三快|龙猫/i.test(s) ? 'meituan'
                     : (/claude|anthropic|opus|sonnet/i.test(s) ? 'claude' : 'generic'),
    _brandSvg: () => '',
    _modelShortName: (id) => id,
    _modelPricingCache: {},
    isChatModel: () => true,
    _compareModelsByDisplayName: (a, b) => String(a).localeCompare(String(b)),
    _sortModelsByDisplayName: (m) => m,
    _sortedBrandKeys: (g) => Object.keys(g).sort(),
    _warnModelCapsMissing: () => {},
    debugLog: () => {},
    t: (k, o) => { let s = k; if (o) for (const q of Object.keys(o)) s += '{' + q + '=' + o[q] + '}'; return s; },
    escapeHtml: (s) => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'),
    CSS: { escape: (s) => s },
    _serverConfig: { hidden_models: [] },
    _hiddenModels: new Set(),
    _stgPresets: {},
    // Seeded below: Api.providers.probeCellsStart / probeCellsStatus
  },
});
const indirectEval = eval;
const VIS_SRC = fs.readFileSync(process.argv[2], 'utf8');

/* Bridge window-published judgment + grouping into node global scope. */
global.foldProbeHealth = window.foldProbeHealth;
global.foldRuntimeHealth = window.foldRuntimeHealth;
global.modelHealthLevelClass = window.modelHealthLevelClass;
global.modelHealthUsable = window.modelHealthUsable;
global.modelGroupKey = window.modelGroupKey;
global.modelGroupLabel = window.modelGroupLabel;
global.modelGroupBrandNames = window.modelGroupBrandNames;

/* Two providers: a normal one (chat ok) and the Anthropic-native face. */
const PROV_A = { id: 'sankuai', name: 'Meituan', base_url: 'https://aigc/v1/openai',
  protocol: '', enabled: true, api_keys: ['k1', 'k2'],
  models: [ { model_id: 'kimi-k3', capabilities: ['text'] } ] };
const PROV_B = { id: 'sankuai_anthropic', name: 'Meituan (Anthropic native)',
  base_url: 'https://aigc/v1/anthropic', protocol: 'anthropic', enabled: true,
  api_keys: ['k1', 'k2'],
  models: [ { model_id: 'claude-opus-4.7', capabilities: ['text'] } ] };
global._stgProviders = window._stgProviders = [PROV_A, PROV_B];
global._getAllModels = window._getAllModels = function () {
  const out = [];
  global._stgProviders.forEach((p, pi) => (p.models || []).forEach((m, mi) =>
    out.push({ model: m, provider: p, provIdx: pi, modelIdx: mi })));
  return out;
};

/* Capture probe bodies + serve canned snapshots. */
const starts = [];
const NOW = Math.floor(Date.now() / 1000);
const SNAP = {
  sankuai: { provider_id: 'sankuai', status: 'done', finished_at: NOW,
    cells: { '0::kimi-k3': { key_idx: 0, model_id: 'kimi-k3', root_model_id: 'kimi-k3', status: 'ok', detail: 'HTTP 200' } } },
  sankuai_anthropic: { provider_id: 'sankuai_anthropic', status: 'done', finished_at: NOW,
    cells: { '0::claude-opus-4.7': { key_idx: 0, model_id: 'aws.opus-4.7', root_model_id: 'claude-opus-4.7', status: 'not_found', detail: 'HTTP 404' },
             '1::claude-opus-4.7': { key_idx: 1, model_id: 'yuju-opus-4.7', root_model_id: 'claude-opus-4.7', status: 'ok', detail: 'HTTP 200' } } },
};
global.Api = window.Api = {
  providers: {
    probeCellsStart: (body) => { starts.push(body); return Promise.resolve(SNAP[body.provider_id]); },
    probeCellsStatus: (pid) => Promise.resolve(SNAP[pid]),
  },
};

function dotFor(mid) {
  return document.querySelector('.stg-dv-health[data-health-for="' + mid + '"] .stg-dv-health-dot');
}

(async () => {
try {
  // ══ 1. Every dropdown row renders a health dot ══
  _renderDropdownVisibility();
  check('dot_for_kimi', dotFor('kimi-k3') !== null);
  check('dot_for_claude', dotFor('claude-opus-4.7') !== null);

  // ══ 2. 测试全部 sends one start per provider, each with its protocol ══
  await _probeAllDropdownModels();
  check('two_starts', starts.length === 2);
  const byProv = {};
  starts.forEach((b) => { byProv[b.provider_id] = b; });
  check('provA_openai_protocol', byProv['sankuai'].protocol === 'openai');
  check('provB_anthropic_protocol', byProv['sankuai_anthropic'].protocol === 'anthropic');
  check('provB_models_sent', (byProv['sankuai_anthropic'].models || [])
    .some((m) => m.model_id === 'claude-opus-4.7'));

  // ══ 3. Dots paint from the shared pool verdict ══
  check('kimi_dot_ok', dotFor('kimi-k3').className.indexOf('mh-ok') >= 0);
  // claude-opus-4.7: 1 ok + 1 not_found → degraded (usable), NOT down.
  const claudeCls = dotFor('claude-opus-4.7').className;
  check('claude_dot_degraded_not_down',
    claudeCls.indexOf('mh-degraded') >= 0 && claudeCls.indexOf('mh-down') < 0);

  // ══ 4. Tooltip carries provider + protocol attribution ══
  const tip = dotFor('claude-opus-4.7').title;
  check('tooltip_has_provider_name', tip.indexOf('Meituan (Anthropic native)') >= 0);
  check('tooltip_has_protocol', tip.indexOf('anthropic') >= 0);
  check('tooltip_shows_failing_wire', tip.indexOf('aws.opus-4.7') >= 0);

  // ══ 5. Stale snapshot paints 'stale', never green ══
  _ddProbeSnaps['sankuai'].snapshot = {
    provider_id: 'sankuai', status: 'done', finished_at: NOW - 3 * 24 * 3600,
    cells: { '0::kimi-k3': { key_idx: 0, model_id: 'kimi-k3', root_model_id: 'kimi-k3', status: 'ok', detail: 'HTTP 200' } },
  };
  _renderDropdownProbeHealth();
  check('stale_dot_not_green', dotFor('kimi-k3').className.indexOf('mh-stale') >= 0);
  check('stale_tooltip_flags_stale', dotFor('kimi-k3').title.indexOf('mhStaleTip') >= 0);

  // ══ NEUTER: drop the provider/protocol attribution ══
  {
    const n = VIS_SRC.replace(
      "var src = cc.provider + (cc.protocol ? ' · ' + cc.protocol : '');",
      "var src = '';");
    check('N1_applied', n !== VIS_SRC);
    indirectEval(n);
    // Re-seed fresh snapshots and repaint under the neutered code.
    _ddProbeSnaps = window._ddProbeSnaps = {};
    await _probeAllDropdownModels();
    const tip2 = dotFor('claude-opus-4.7').title;
    check('N1_attribution_lost', tip2.indexOf('Meituan (Anthropic native)') < 0);
    indirectEval(VIS_SRC);   // restore
  }
} catch (e) {
  check('harness_threw: ' + (e && e.message), false);
} finally {
  report();
}
})();
'''


def test_dropdown_probe_health():
    body = _BODY.replace('HTML_PLACEHOLDER', json.dumps(_HTML))
    run_harness(
        target_js=VISIBILITY_JS,
        body_js=body,
        extra_targets=[MODEL_HEALTH_JS, MODEL_GROUP_JS],
        min_pass=15,
        label='dropdown-probe-health',
    )


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
