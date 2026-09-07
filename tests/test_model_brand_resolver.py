#!/usr/bin/env python3
"""One brand-resolution interface for every model surface, Creator first.

``createModelBrandResolver`` (frontend/src/core/model-brand-detection.ts) is
the single entry point: an explicit Creator id wins, the registered-models
catalog covers callers holding only a model id, and name-pattern matching is
the fallback for ids the catalog has never seen. The regression this pins:
official Meta models like ``meta/esmfold`` or ``meta/muse-spark-1.2`` carry
no family token in their name, so pattern-only resolution rendered the grey
generic box even though the configuration already states creator = Meta.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import (
    ROOT,
    native_module_path,
    runtime_section,
)

pytestmark = pytest.mark.unit

NODE = shutil.which('node')


def _run_node(harness_path, *args):
    result = subprocess.run(
        [NODE, str(harness_path), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.skipif(not NODE, reason='node not installed')
def test_brand_resolver_prioritizes_creator_over_name_pattern(tmp_path):
    module = native_module_path(
        '.native/model-brand-detection.js',
        ROOT / 'frontend/src/core/model-brand-detection.ts',
    )
    harness = tmp_path / 'harness.js'
    harness.write_text(r'''
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
const catalog = { 'esmfold': 'meta', 'muse-spark-1.2': 'meta', 'kimi-k2': 'mystery' };
const resolver = createModelBrandResolver({
  lookupCreatorId: (id) => catalog[id] || '',
});
const brand = resolver.modelBrand;
console.log(JSON.stringify({
  creatorMeta: brandForCreator('meta'),
  creatorNormalized: brandForCreator('Moonshot AI'),
  creatorUnknown: brandForCreator('mystery-lab'),
  creatorNull: brandForCreator(null),
  hintWins: brand('esmfold', 'meta'),
  hintBeatsName: brand('gpt-4o', 'anthropic'),
  catalogLookup: brand('esmfold'),
  catalogLookupSpark: brand('muse-spark-1.2'),
  patternFallback: brand('meta/llama-3.1-8b-instruct'),
  patternFromUnmappedCreator: brand('kimi-k2'),
  unknownId: brand('totally-unknown-xyz'),
  emptyId: brand(''),
  nullId: brand(null),
  patternIntact: detectModelBrand('meta/llama-3.1-8b-instruct'),
  tableFrozen: Object.isFrozen(CREATOR_TO_BRAND),
  tableKeysNormalized: Object.keys(CREATOR_TO_BRAND).every((k) => /^[a-z0-9]+$/.test(k)),
}));
''', encoding='utf-8')
    values = json.loads(_run_node(harness, module).strip().splitlines()[-1])
    # Creator id → glyph table: normalized ids only, no wrong-glyph guesses.
    assert values['creatorMeta'] == 'meta'
    assert values['creatorNormalized'] == 'kimi'
    assert values['creatorUnknown'] == ''
    assert values['creatorNull'] == ''
    # Resolution order: explicit hint > catalog lookup > name pattern.
    assert values['hintWins'] == 'meta'
    assert values['hintBeatsName'] == 'claude'
    assert values['catalogLookup'] == 'meta'
    assert values['catalogLookupSpark'] == 'meta'
    assert values['patternFallback'] == 'meta'
    assert values['patternFromUnmappedCreator'] == 'kimi'
    assert values['unknownId'] == 'generic'
    assert values['emptyId'] == 'generic'
    assert values['nullId'] == 'generic'
    # The pattern detector itself keeps its legacy behaviour as fallback.
    assert values['patternIntact'] == 'meta'
    assert values['tableFrozen'] is True
    assert values['tableKeysNormalized'] is True


@pytest.mark.skipif(not NODE, reason='node not installed')
def test_vendor_icon_follows_creator_identity(tmp_path):
    entry = tmp_path / 'entry.ts'
    output = tmp_path / 'vendor.cjs'
    module = ROOT / 'frontend/src/features/model-catalog/vendor.ts'
    entry.write_text(f'''
import {{ detectVendor }} from {json.dumps(module.as_posix())};
console.log(JSON.stringify({{
  esmfold: detectVendor('meta', 'Meta', 'esmfold'),
  spark: detectVendor('meta', 'Meta', 'muse-spark-1.2'),
  fallback: detectVendor('mystery', 'Mystery Labs', 'llama-3-8b'),
  tokenless: detectVendor('mystery', 'Mystery Labs', 'tokenless'),
  kimi: detectVendor('moonshot', 'Moonshot AI', 'kimi-k2'),
}}));
''', encoding='utf-8')
    bundled = subprocess.run(
        [NODE, str(ROOT / 'scripts/vite_test_bundle.mjs'), str(entry),
         '--bundle', '--format=cjs', '--platform=node', f'--outfile={output}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert bundled.returncode == 0, bundled.stderr
    values = json.loads(_run_node(output).strip().splitlines()[-1])
    # The screenshot regression: a name without any family token still gets
    # the Meta glyph because the catalog Creator says so.
    assert values['esmfold']['icon'] == 'meta'
    assert values['spark']['icon'] == 'meta'
    # Icon change must not leak into vendor grouping: id/label stay put.
    assert values['esmfold']['id'] == 'meta'
    assert values['esmfold']['label'] == 'Meta'
    # Unmapped Creators keep the pattern fallback — and never a wrong glyph.
    assert values['fallback']['icon'] == 'meta'
    assert values['tokenless']['icon'] == 'generic'
    assert values['kimi']['icon'] == 'kimi'


@pytest.mark.skipif(not NODE, reason='node not installed')
def test_access_matrix_row_icon_resolves_via_model_brand(tmp_path):
    module = native_module_path(
        '.native/model-brand-detection.js',
        ROOT / 'frontend/src/core/model-brand-detection.ts',
    )
    provider = tmp_path / 'provider-render.js'
    provider.write_text(runtime_section('settings/provider_render.js'), encoding='utf-8')
    matrix = tmp_path / 'access-matrix.js'
    matrix.write_text(runtime_section('settings/access_matrix.js'), encoding='utf-8')
    harness = tmp_path / 'harness.js'
    harness.write_text(r'''
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.escapeHtml = value => String(value == null ? '' : value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');
global.t = key => key;
global._detectBrand = () => 'generic';
global._brandSvg = brand => '<i data-brand="' + brand + '"></i>';
global._stgPendingCredentialSecrets = {};
global._stgModelRoutingLoadError = '';
global.document = {
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
};
global.Api = { modelRouting: { probeCellsStatus: () => new Promise(() => {}) } };
global._stgModelRouting = {
  contract_version: 'tofu.model-routing/v2',
  providers: [{provider_id: 'p1', name: 'Gateway', scope: 'owner', brand: 'generic'}],
  provider_accesses: [{provider_access_id: 'p1-access', provider_id: 'p1',
    display_name: 'Gateway', enabled: true, quota_policy: {}}],
  connections: [{connection_id: 'p1-conn', provider_access_id: 'p1-access',
    base_url: 'https://gw.example/v1', protocol: 'openai', enabled: true}],
  credentials: [{credential_id: 'cred-a', provider_access_id: 'p1-access', kind: 'api_key',
    enabled: true, authorization: {connection_ids: ['p1-conn'], models: []}}],
  models: [],
  offerings: [{offering_id: 'off-0', provider_access_id: 'p1-access', identity_state: 'confirmed',
    model: {creator_id: 'meta', model_id: 'esmfold'}, enabled: true, stale: false,
    capabilities: ['text'], context_window: 1000}],
  deployments: [{deployment_id: 'dep-0', offering_id: 'off-0', connection_id: 'p1-conn',
    wire_model_id: 'esmfold', enabled: true, identity_confidence: 'high', probe_status: 'passed'}],
};
eval(fs.readFileSync(process.argv[5], 'utf8')); // native model-brand module → globalThis
const brandCalls = [];
const realResolver = createModelBrandResolver({ lookupCreatorId: () => '' });
global._modelBrand = (id, creator) => {
  brandCalls.push([id, creator]);
  return realResolver.modelBrand(id, creator);
};
eval(fs.readFileSync(process.argv[2], 'utf8'));
eval(fs.readFileSync(process.argv[4], 'utf8'));
const html = _renderAccessMatrix('p1');
console.log(JSON.stringify({html: html, brandCalls: brandCalls}));
''', encoding='utf-8')
    output = json.loads(_run_node(harness, provider, '', matrix, module))
    # The wire id 'esmfold' has no family token; the row icon must come from
    # the offering's Creator identity passed as the resolver hint.
    assert output['brandCalls'] == [['esmfold', 'meta']]
    assert output['html'].count('data-brand="meta"') == 1


def test_model_brand_resolver_wiring_source_pins():
    detection = (
        ROOT / 'frontend/src/core/model-brand-detection.ts'
    ).read_text(encoding='utf-8')
    assert 'export const CREATOR_TO_BRAND' in detection
    assert 'export function brandForCreator' in detection
    assert 'export function createModelBrandResolver' in detection

    # The composed runtime owns the one resolver instance: catalog lookup is
    # fed by _registeredModels, and the epilogue publishes it to lazy bundles.
    sections_dir = ROOT / 'frontend/src/runtime/sections'
    prelude = (sections_dir / '_prelude.js').read_text(encoding='utf-8')
    assert 'createModelBrandResolver({' in prelude
    assert 'lookupCreatorId' in prelude
    assert '_registeredModels' in prelude
    assert 'const _modelBrand = modelBrandResolver.modelBrand;' in prelude
    epilogue = (sections_dir / '_epilogue.js').read_text(encoding='utf-8')
    assert '\n  _modelBrand,\n' in epilogue

    # Every lazy bundle that still carries the legacy _detectBrand fallback
    # must also receive the resolver, or its surfaces silently downgrade.
    manifest = json.loads(
        (ROOT / 'frontend/src/runtime/sections/manifest.json').read_text(
            encoding='utf-8'))
    offenders = []
    for bundle in manifest['lazyBundles']:
        names = [entry['name'] for entry in bundle.get('runtimeServices', [])]
        if '_detectBrand' in names and '_modelBrand' not in names:
            offenders.append(bundle.get('output', '?'))
    assert offenders == []

    # Every model-logo call site goes through the unified interface (the
    # _detectBrand fallback stays only as an isolated-harness escape hatch).
    for section in (
        'settings/provider_render.js',
        'settings/access_matrix.js',
        'image-gen.js',
        'ui/finish_info.js',
        'info-rail.js',
        'main.js',
        'main/main_toolbar_ui.js',
    ):
        source = runtime_section(section, scope_prelude=False)
        assert '_modelBrand(' in source, section

    vendor = (
        ROOT / 'frontend/src/features/model-catalog/vendor.ts'
    ).read_text(encoding='utf-8')
    assert 'brandForCreator(creatorId)' in vendor
