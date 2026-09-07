#!/usr/bin/env python3
"""Typed model-display folding and bounded recent-model persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import (
    native_module_path,
    runtime_section,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
FOLD_JS = native_module_path(
    '.native/model-display-fold.js',
    ROOT / 'frontend/src/core/model-display-fold.ts',
)
RECENTS_JS = native_module_path(
    '.native/recent-models.js',
    ROOT / 'frontend/src/core/recent-models.ts',
)

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2], process.argv[4]],
  globals: {},
});

const persisted = {};
const fakeStorage = {
  getItem: (key) => key in persisted ? persisted[key] : null,
  setItem: (key, value) => { persisted[key] = String(value); },
};
const recentModelsController = createRecentModelsController({
  resolveStorage: () => fakeStorage,
});
Object.assign(global, recentModelsController);
Object.assign(window, recentModelsController);

const model = (id, extra) => Object.assign({ model_id: id }, extra || {});

try {
  check('malformed_catalog_is_empty', modelDisplayUnits(null).length === 0);

  const plainInput = [model('a'), model('b')];
  const plainBefore = JSON.stringify(plainInput);
  let units = modelDisplayUnits(plainInput);
  check('plain_entries_stay_single',
    units.length === 2 && units.every((unit) => unit.kind === 'single'));
  check('fold_does_not_mutate_input', JSON.stringify(plainInput) === plainBefore);

  units = modelDisplayUnits([
    model('deepseek-v3.2', {
      fold_group: 'p:deepseek-v3.2', fold_canonical: 'deepseek-v3.2',
    }),
    model('deepseek-v3.2-baidu', {
      fold_group: 'p:deepseek-v3.2', fold_canonical: 'deepseek-v3.2',
    }),
    model('kimi-k3'),
  ]);
  check('alias_unit_count', units.length === 2);
  check('alias_unit_kind', units[0].kind === 'alias');
  check('alias_face_is_canonical',
    units[0].face.model_id === 'deepseek-v3.2');
  check('alias_members_remain_reachable', units[0].members.length === 2);

  units = modelDisplayUnits([
    model('glm-5.1', { family: 'p:glm', family_primary: 'glm-5.3' }),
    model('glm-5.2', { family: 'p:glm', family_primary: 'glm-5.3' }),
    model('glm-5.3', { family: 'p:glm', family_primary: 'glm-5.3' }),
  ]);
  check('family_is_one_unit', units.length === 1 && units[0].kind === 'family');
  check('family_face_is_primary', units[0].face.model_id === 'glm-5.3');
  check('family_children_remain_reachable', units[0].children.length === 3);

  units = modelDisplayUnits([
    model('glm-5.1', { family: 'p:glm', family_primary: 'glm-5.3' }),
    model('glm-5.2', { family: 'p:glm', family_primary: 'glm-5.3' }),
  ]);
  check('missing_primary_degrades_to_first_visible',
    units.length === 1 && units[0].face.model_id === 'glm-5.1');

  units = modelDisplayUnits([
    model('glm-5.1', {
      fold_group: 'p:glm-5.1', fold_canonical: 'glm-5.1',
      family: 'p:glm', family_primary: 'glm-5.3',
    }),
    model('glm-5.1-huawei', {
      fold_group: 'p:glm-5.1', fold_canonical: 'glm-5.1',
    }),
    model('glm-5.3', { family: 'p:glm', family_primary: 'glm-5.3' }),
  ]);
  check('alias_can_nest_in_family', units.length === 1 && units[0].kind === 'family');
  check('nested_alias_mirror_survives',
    units[0].members.some((entry) => entry.model_id === 'glm-5.1-huawei'));

  ['a', 'b', 'c', 'a', 'd', 'e', 'f'].forEach(pushRecentModel);
  const recent = recentModels();
  check('recents_are_deduplicated', recent.filter((id) => id === 'a').length === 1);
  check('recents_are_most_recent_first', recent[0] === 'f');
  check('recents_are_bounded', recent.length === RECENT_MODELS_MAX);
  recent.length = 0;
  check('recent_snapshot_is_defensive',
    recentModels().length === RECENT_MODELS_MAX);

  const savedSetItem = fakeStorage.setItem;
  fakeStorage.setItem = () => { throw new Error('quota'); };
  pushRecentModel('quota-does-not-break-picker');
  fakeStorage.setItem = savedSetItem;
  check('quota_failure_preserves_readability',
    recentModels().length === RECENT_MODELS_MAX);

  fakeStorage.setItem(RECENT_MODELS_STORAGE_KEY,
    JSON.stringify(['one', 7, '', 'two', 'three', 'four', 'five', 'six']));
  check('persisted_values_are_validated_and_bounded',
    JSON.stringify(recentModels()) === '["one","two","three","four","five"]');

  fakeStorage.setItem(RECENT_MODELS_STORAGE_KEY, '{not-json');
  check('corrupt_storage_fails_open', recentModels().length === 0);

  fakeStorage.setItem(RECENT_MODELS_STORAGE_KEY, '["stable"]');
  const beforeInvalidPush = fakeStorage.getItem(RECENT_MODELS_STORAGE_KEY);
  pushRecentModel(7);
  check('invalid_model_id_is_not_persisted',
    fakeStorage.getItem(RECENT_MODELS_STORAGE_KEY) === beforeInvalidPush);
  check('recent_controller_is_immutable', Object.isFrozen(recentModelsController));
} catch (error) {
  check('harness_threw: ' + (error && error.message), false);
} finally {
  report();
}
'''


def test_model_display_units_and_recent_models():
    run_harness(
        target_js=FOLD_JS,
        body_js=_BODY,
        extra_targets=[RECENTS_JS],
        expect_pass=22,
        label='typed-model-display-fold',
    )


def test_consumers_route_through_the_typed_fold_owner():
    """Only display surfaces fold; per-model visibility remains flat."""
    toolbar = runtime_section('main/main_toolbar_ui.js')
    assert 'modelDisplayUnits' in toolbar, (
        'picker no longer uses the typed model-display fold')
    assert '_filterModelDropdown' in toolbar, (
        'picker lost its model search filter')
    providers = runtime_section('settings/provider_render.js')
    assert ('_renderProviderModelList' in providers
            and 'modelDisplayUnits' in providers), (
        'providers tab no longer folds version families')
    visibility = runtime_section('settings/visibility_defaults.js')
    assert ('modelDisplayUnits' not in visibility
            and '_stgDvUnits' not in visibility), (
        'preset visibility lists must keep every model independently togglable')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
