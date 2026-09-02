#!/usr/bin/env python3
"""Frontend test — the PROVIDERS tab folds version families (and ONLY there).

WHY
---
Owner ruling 2026-08-23: gateway endpoints expose a wall of near-identical
version rows (gemini-3-flash-preview / 3.5-flash / 3.6-flash …). The fold
belongs in the providers tab's model-card list — NOT the preset visibility
lists, where every model_id must stay an individually toggleable row.

WHAT IS GUARDED (results, not implementation)
---------------------------------------------
  * A family (shared ``family`` metadata) renders ONE face card (the
    family_primary) + a "还有 N 个版本" expander + the remaining versions as
    full cards inside the collapsed sub-container.
  * Cards keep their REAL model indices (data-model attr) so enable/edit/
    delete/alias actions still target the right entry — folded or not.
  * Alias mirrors are NOT merged on this management surface: two entries of
    one fold_group still render as two cards.
  * Expander state survives a re-render (keyed by provider::face).
  * No metadata (stale payload) → the exact old flat list.

NEUTER: route every unit through the flat branch → the family wall returns
(red), proving the fold branch is load-bearing.
"""

from __future__ import annotations

import json

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

PROVIDER_RENDER_JS = runtime_section_path('settings/provider_render.js')
FOLD_JS = runtime_section_path('core/model_fold.js')

_HTML = '<!DOCTYPE html><body></body>'

_BODY = r'''
const fs = require('fs');
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, window, check, report } = setup({
  root: process.argv[3],
  html: HTML_PLACEHOLDER,
  targets: [process.argv[2], process.argv[4]],
  globals: {
    _detectBrand: () => 'generic',
    _brandSvg: () => '',
    _modelPricingCache: {},
    t: (k, o) => { let s = k; if (o) for (const q of Object.keys(o)) s += '{' + q + '=' + o[q] + '}'; return s; },
    escapeHtml: (s) => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),
    debugLog: () => {},
  },
});
const indirectEval = eval;
const PR_SRC = fs.readFileSync(process.argv[2], 'utf8');

/* Bridge the fold module + the helpers under test into node global scope. */
global.modelDisplayUnits = window.modelDisplayUnits;
global._toggleStgProvFold = window._toggleStgProvFold;

/* One gateway provider: a 3-version gemini-flash family + a singleton. */
const P = { id: 'p1', name: 'GW', base_url: 'https://gw/v1', enabled: true,
  api_keys: ['k1'],
  models: [
    { model_id: 'gemini-3-flash-preview', capabilities: ['text'] },
    { model_id: 'gemini-3.5-flash', capabilities: ['text'] },
    { model_id: 'gemini-3.6-flash', capabilities: ['text'] },
    { model_id: 'kimi-k3', capabilities: ['text'] },
  ] };
global._stgProviders = window._stgProviders = [P];
global._serverConfig = window._serverConfig = { model_folds: {
  'p1::gemini-3-flash-preview': { family: 'p1:gemini-flash', family_primary: 'gemini-3.6-flash' },
  'p1::gemini-3.5-flash': { family: 'p1:gemini-flash', family_primary: 'gemini-3.6-flash' },
  'p1::gemini-3.6-flash': { family: 'p1:gemini-flash', family_primary: 'gemini-3.6-flash' },
} };

function render() {
  const html = _renderProviderModelList(0, P, P.models);
  const host = document.createElement('div');
  host.innerHTML = html;
  document.body.appendChild(host);
  return host;
}
function cards(host) {
  return Array.from(host.querySelectorAll('.stg-mcard'))
    .map((el) => el.getAttribute('data-model'));
}

try {
  // ══ 1. Family folds under the primary; every card keeps its REAL index ══
  {
    const host = render();
    const expanders = host.querySelectorAll('.stg-mcard-fold');
    check('one_expander', expanders.length === 1);
    check('expander_counts_two_more',
      expanders[0].textContent.indexOf('count=2') >= 0);
    check('face_is_primary_real_index',
      host.querySelector('.stg-model-list > .stg-mcard').getAttribute('data-model') === '2');
    const sub = host.querySelector('.stg-mcard-fold-sub');
    check('sub_exists_collapsed', sub && !sub.classList.contains('open'));
    const subIdx = Array.from(sub.querySelectorAll('.stg-mcard'))
      .map((el) => el.getAttribute('data-model'));
    check('sub_holds_older_versions', JSON.stringify(subIdx) === '["0","1"]');
    // 4 cards total: face + 2 folded + kimi single, all real indices.
    check('all_cards_real_indices',
      JSON.stringify(cards(host).slice().sort()) === '["0","1","2","3"]');
    host.remove();
  }

  // ══ 2. Expander toggles and REMEMBERS open state across re-renders ══
  {
    const host = render();
    const exp = host.querySelector('.stg-mcard-fold');
    _toggleStgProvFold(exp);
    check('toggle_opens_sub',
      host.querySelector('.stg-mcard-fold-sub').classList.contains('open'));
    host.remove();
    const host2 = render();   // re-render (alias add / matrix toggle / …)
    check('open_state_survives_rerender',
      host2.querySelector('.stg-mcard-fold-sub').classList.contains('open')
      && host2.querySelector('.stg-mcard-fold').classList.contains('open'));
    _toggleStgProvFold(host2.querySelector('.stg-mcard-fold'));   // close again
    host2.remove();
    const host3 = render();
    check('closed_state_survives_rerender',
      !host3.querySelector('.stg-mcard-fold-sub').classList.contains('open'));
    host3.remove();
  }

  // ══ 3. Alias mirrors are NOT merged on this management surface ══
  {
    const PA = { id: 'p2', name: 'GW2', models: [
      { model_id: 'deepseek-v3.2', capabilities: ['text'] },
      { model_id: 'deepseek-v3.2-baidu', capabilities: ['text'] },
    ] };
    global._stgProviders = window._stgProviders = [P, PA];
    window._serverConfig.model_folds = {
      'p2::deepseek-v3.2': { fold_group: 'p2:deepseek-v3.2', fold_canonical: 'deepseek-v3.2' },
      'p2::deepseek-v3.2-baidu': { fold_group: 'p2:deepseek-v3.2', fold_canonical: 'deepseek-v3.2' },
    };
    const host = (() => {
      const html = _renderProviderModelList(1, PA, PA.models);
      const el = document.createElement('div'); el.innerHTML = html; return el;
    })();
    check('alias_members_stay_flat_cards', cards(host).length === 2);
    check('alias_no_expander', host.querySelectorAll('.stg-mcard-fold').length === 0);
  }

  // ══ 4. No metadata → exact old flat list ══
  {
    window._serverConfig.model_folds = {};
    const host = (() => {
      const html = _renderProviderModelList(0, P, P.models);
      const el = document.createElement('div'); el.innerHTML = html; return el;
    })();
    check('no_metadata_flat', cards(host).length === 4
      && host.querySelectorAll('.stg-mcard-fold').length === 0);
  }

  // ══ NEUTER: route every unit through the flat branch → the wall returns ══
  {
    window._serverConfig.model_folds = {
      'p1::gemini-3-flash-preview': { family: 'p1:gemini-flash', family_primary: 'gemini-3.6-flash' },
      'p1::gemini-3.5-flash': { family: 'p1:gemini-flash', family_primary: 'gemini-3.6-flash' },
      'p1::gemini-3.6-flash': { family: 'p1:gemini-flash', family_primary: 'gemini-3.6-flash' },
    };
    const n = PR_SRC.replace("if (!u || u.kind !== 'family') {",
      "if (true) {");
    check('N1_applied', n !== PR_SRC);
    indirectEval(n);
    const host = (() => {
      const html = _renderProviderModelList(0, P, P.models);
      const el = document.createElement('div'); el.innerHTML = html; return el;
    })();
    check('N1_wall_returns', cards(host).length === 4
      && host.querySelectorAll('.stg-mcard-fold').length === 0);
    indirectEval(PR_SRC);   // restore
  }
} catch (e) {
  check('harness_threw: ' + (e && e.message), false);
} finally {
  report();
}
'''


def test_provider_model_fold():
    body = _BODY.replace('HTML_PLACEHOLDER', json.dumps(_HTML))
    run_harness(
        target_js=PROVIDER_RENDER_JS,
        body_js=body,
        extra_targets=[FOLD_JS],
        min_pass=13,
        label='provider-model-fold',
    )


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
