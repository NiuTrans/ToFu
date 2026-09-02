#!/usr/bin/env python3
"""Frontend test — core/model_fold.js display units + picker integration.

WHY
---
Gateway endpoints (Meituan) expose dozens of rows that are really ONE thing
(cloud mirrors) or ONE line (glm-5.1/5.2/5.3). core/model_fold.js is the
single rule that turns a flat model list into render units for BOTH the
toolbar picker and the Settings visibility lists. A regression here either
hides configured models (wrong fold) or strands the picker in a wall of
rows (fold silently dropped).

WHAT IS GUARDED (results, not implementation)
---------------------------------------------
  * Entries with the same fold_group collapse to ONE alias unit faced by
    fold_canonical.
  * Units sharing a family collapse to ONE family unit faced by
    family_primary; a hidden primary degrades to the first visible unit.
  * An alias unit inside a family keeps its mirrors reachable (nesting).
  * Entries without metadata pass through as singles (stale payload).
  * recentModels/pushRecentModel dedupe, cap at 5, and survive broken
    localStorage.
  * INTEGRATION PIN: the toolbar picker section actually routes through
    modelDisplayUnits and ships the search filter (a rewired-away call
    would leave every fold inert).

NEUTER: drop the family pass → family units dissolve into singles (red).
"""

from __future__ import annotations

import json

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import runtime_section, runtime_section_path

pytestmark = pytest.mark.unit

FOLD_JS = runtime_section_path('core/model_fold.js')

_HTML = '<!DOCTYPE html><body></body>'

_BODY = r'''
const fs = require('fs');
const { setup } = require(process.env.JSDOM_HARNESS);
/* The harness evals targets in NODE global scope, so the module's bare
 * `localStorage` resolves to a Node global — NOT jsdom's window.localStorage.
 * Seed an in-memory fake (the production contract: getItem/setItem throwing
 * must never break the picker). */
const _store = {};
const fakeLS = {
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
  removeItem: (k) => { delete _store[k]; },
  clear: () => { for (const k of Object.keys(_store)) delete _store[k]; },
};
const { window, check, report } = setup({
  root: process.argv[3],
  html: HTML_PLACEHOLDER,
  targets: [process.argv[2]],
  globals: {},
});
/* window.localStorage is a getter-only accessor (jsdom) so it cannot ride
 * setup()'s globals map. The module resolves bare `localStorage` from NODE
 * global scope at CALL time, so attaching the fake post-eval still wires it. */
global.localStorage = fakeLS;
const indirectEval = eval;
const FOLD_SRC = fs.readFileSync(process.argv[2], 'utf8');
const modelDisplayUnits = window.modelDisplayUnits;
const recentModels = window.recentModels;
const pushRecentModel = window.pushRecentModel;

const M = (id, extra) => Object.assign({ model_id: id }, extra || {});

try {
  // ══ 1. Flat list without metadata → all singles ══
  let units = modelDisplayUnits([M('a'), M('b')]);
  check('plain_singles', units.length === 2 && units.every(u => u.kind === 'single'));

  // ══ 2. Alias fold: one unit, canonical face ══
  units = modelDisplayUnits([
    M('deepseek-v3.2', { fold_group: 'p:deepseek-v3.2', fold_canonical: 'deepseek-v3.2' }),
    M('deepseek-v3.2-baidu', { fold_group: 'p:deepseek-v3.2', fold_canonical: 'deepseek-v3.2' }),
    M('kimi-k3'),
  ]);
  check('alias_unit_count', units.length === 2);
  check('alias_kind', units[0].kind === 'alias');
  check('alias_face_is_canonical', units[0].face.model_id === 'deepseek-v3.2');
  check('alias_members', units[0].members.length === 2);

  // ══ 3. Family fold: versions collapse under the primary ══
  units = modelDisplayUnits([
    M('glm-5.1', { family: 'p:glm', family_primary: 'glm-5.3' }),
    M('glm-5.2', { family: 'p:glm', family_primary: 'glm-5.3' }),
    M('glm-5.3', { family: 'p:glm', family_primary: 'glm-5.3' }),
  ]);
  check('family_single_unit', units.length === 1 && units[0].kind === 'family');
  check('family_face_is_primary', units[0].face.model_id === 'glm-5.3');
  check('family_children', (units[0].children || []).length === 3);

  // ══ 4. Primary filtered out → first visible unit fronts (degrade) ══
  units = modelDisplayUnits([
    M('glm-5.1', { family: 'p:glm', family_primary: 'glm-5.3' }),
    M('glm-5.2', { family: 'p:glm', family_primary: 'glm-5.3' }),
  ]);
  check('family_degraded_face', units.length === 1
    && units[0].face.model_id === 'glm-5.1');

  // ══ 5. Alias unit nested in a family keeps mirrors reachable ══
  units = modelDisplayUnits([
    M('glm-5.1', { fold_group: 'p:glm-5.1', fold_canonical: 'glm-5.1',
                   family: 'p:glm', family_primary: 'glm-5.3' }),
    M('glm-5.1-huawei', { fold_group: 'p:glm-5.1', fold_canonical: 'glm-5.1' }),
    M('glm-5.3', { family: 'p:glm', family_primary: 'glm-5.3' }),
  ]);
  check('nested_family', units.length === 1 && units[0].kind === 'family');
  check('nested_mirror_survives',
    units[0].members.some(m => m.model_id === 'glm-5.1-huawei'));

  // ══ 6. Recents: dedupe + cap + order ══
  fakeLS.clear();
  ['a', 'b', 'c', 'a', 'd', 'e', 'f'].forEach(pushRecentModel);
  const rec = recentModels();
  check('recent_deduped', rec.filter(x => x === 'a').length === 1);
  check('recent_mru_first', rec[0] === 'f');
  check('recent_capped', rec.length === 5);

  // ══ 7. Broken localStorage never throws ══
  const origSet = fakeLS.setItem;
  fakeLS.setItem = () => { throw new Error('quota'); };
  pushRecentModel('zzz');
  fakeLS.setItem = origSet;
  check('recent_survives_quota', recentModels().length === 5);

  // ══ NEUTER: drop the family pass → family units dissolve ══
  {
    const n = FOLD_SRC.replace(
      "if (f && f.family) {", "if (false) {");
    check('N1_applied', n !== FOLD_SRC);
    indirectEval(n);
    const nUnits = window.modelDisplayUnits([
      M('glm-5.1', { family: 'p:glm', family_primary: 'glm-5.3' }),
      M('glm-5.3', { family: 'p:glm', family_primary: 'glm-5.3' }),
    ]);
    check('N1_family_dissolved',
      nUnits.length === 2 && nUnits.every(u => u.kind === 'single'));
    indirectEval(FOLD_SRC);  // restore
  }
} catch (e) {
  check('harness_threw: ' + (e && e.message), false);
} finally {
  report();
}
'''


def test_model_display_units():
    body = _BODY.replace('HTML_PLACEHOLDER', json.dumps(_HTML))
    run_harness(
        target_js=FOLD_JS,
        body_js=body,
        min_pass=14,
        label='model-fold-units',
    )


def test_picker_routes_through_fold_module():
    """Integration pins — folds live ONLY where the owner put them
    (ruling 2026-08-23): the toolbar picker folds; the PROVIDERS tab card
    list folds version families; the preset visibility lists must NOT fold
    (they are the management surface — every model_id stays an individually
    toggleable row there)."""
    toolbar = runtime_section('main/main_toolbar_ui.js')
    assert 'modelDisplayUnits' in toolbar, (
        'picker no longer folds through core/model_fold.js')
    assert '_filterModelDropdown' in toolbar, (
        'picker lost its model search filter')
    providers = runtime_section('settings/provider_render.js')
    assert ('_renderProviderModelList' in providers
            and 'modelDisplayUnits' in providers), (
        'providers tab no longer folds version families')
    visibility = runtime_section('settings/visibility_defaults.js')
    assert ('modelDisplayUnits' not in visibility
            and '_stgDvUnits' not in visibility), (
        'preset visibility lists re-grew a display fold — owner ruling '
        '2026-08-23 keeps every model a flat, individually toggleable row')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
