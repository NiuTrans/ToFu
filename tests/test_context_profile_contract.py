"""Unit contracts for structured context-window knowledge and UI semantics."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_unknown_new_catalogue_models_are_not_fabricated():
    from lib.model_info import context_profile

    # gemini-3.5-pro / -ultra also pin the post-rule unknown guard: verified
    # flash variants win earlier, but unknown future variants must NOT
    # inherit the generic 'gemini' family estimate.
    for model in ('gemini-3.5-pro', 'gemini-3.6-ultra', 'gemini-3.7-pro',
                  'glm-6', 'kimi-k4'):
        assert context_profile(model) == {
            'window': None, 'source': 'unknown', 'exact': False,
        }


def test_sankuai_catalog_models_promoted_to_vendor_windows():
    """2026-08-14 owner-approved promotion: vendor-documented windows moved
    from the runtime-data backfill into static knowledge, so fresh installs
    and template setups resolve them without a seeded server_config."""
    from lib.model_info import context_profile

    exact = {
        'gemini-3.5-flash': 1_000_000,
        'gemini-3.5-flash-lite': 1_000_000,
        'gemini-3.6-flash': 1_000_000,
        'gemini-3.7-flash': 1_000_000,
        'glm-5.1': 200_000,
        'glm-5.2': 1_000_000,
        'glm-5.3': 1_000_000,
        'glm-5v-turbo': 200_000,
        'hy3-preview': 256_000,
        'LongCat-2.0': 1_000_000,
        'text-embedding-3-large': 8_191,
        'text-embedding-3-small': 8_191,
        'text-embedding-v4': 8_192,
    }
    for model, window in exact.items():
        assert context_profile(model) == {
            'window': window, 'source': 'vendor_official', 'exact': True,
        }, model
    for model in ('LongCat-Flash-Thinking-2601', 'LongCat-Flash-Omni-2603'):
        assert context_profile(model) == {
            'window': 128_000, 'source': 'family_estimate', 'exact': False,
        }, model


def test_gpt56_context_window_is_official_and_exact():
    from lib.model_info import context_profile

    for model in ('gpt-5.6', 'gpt-5.6-sol', 'gpt-5.6-terra',
                  'gpt-5.6-luna'):
        assert context_profile(model) == {
            'window': 1_050_000,
            'source': 'openai_official',
            'exact': True,
        }


def test_codex_catalog_models_have_vendor_windows():
    """oauth_codex rows are rebuilt from the remote catalogue, so their
    windows must resolve from static knowledge (vendor-documented)."""
    from lib.model_info import context_profile

    expected = {
        'gpt-5.5': (1_050_000, 'vendor_official', True),
        'gpt-5.4': (1_000_000, 'vendor_official', True),
        'gpt-5.4-mini': (400_000, 'vendor_official', True),
        'gpt-5.3-codex-spark': (128_000, 'vendor_official', True),
        # Watermark serving variant of the official 1.05M contract id.
        'gpt-5.6-sol-wm': (1_050_000, 'family_estimate', False),
    }
    for model, (window, source, exact) in expected.items():
        assert context_profile(model) == {
            'window': window, 'source': source, 'exact': exact,
        }


def test_verified_and_estimated_registry_values_are_distinguishable():
    from lib.model_info import context_profile

    assert context_profile('kimi-k3') == {
        'window': 1_000_000, 'source': 'repository_verified', 'exact': True,
    }
    estimated = context_profile('MiniMax-M3')
    assert estimated['window'] == 1_000_000
    assert estimated['source'] == 'family_estimate'
    assert estimated['exact'] is False


def test_compaction_unknown_fallback_does_not_pollute_profile():
    from lib.model_info import context_profile
    from lib.tasks_pkg.compaction._tokens import _get_static_context_limit

    model = 'future-model-with-no-evidence'
    assert context_profile(model)['window'] is None
    assert _get_static_context_limit({'config': {'model': model}}) > 0
    assert context_profile(model)['window'] is None


def test_stale_expand_never_pins_below_static_window(monkeypatch):
    """kimi-k3 regression (2026-08-14): a stale expand observation (a
    365K-prompt request succeeded on 2026-07-26, learned as 383,727) must
    not mask the repository-verified 1M window at the profile layer."""
    import lib.context_limits as limits
    from lib.model_info import resolved_context_profile

    monkeypatch.setattr(limits, '_LEARNED', {'sankuai::kimi-k3': 383_727})
    monkeypatch.setattr(limits, '_META', {
        'sankuai::kimi-k3': {'source': 'expand', 'ts': 0, 'strikes': 0},
    })
    assert resolved_context_profile('kimi-k3', 'sankuai') == {
        'window': 1_000_000, 'source': 'repository_verified', 'exact': True,
    }
    # An expand ABOVE static still surfaces (the gateway really accepted more).
    monkeypatch.setattr(limits, '_LEARNED', {'sankuai::claude-opus-5': 1_110_553})
    assert resolved_context_profile('claude-opus-5', 'sankuai')['window'] == 1_110_553


def test_config_save_never_bakes_learned_context(monkeypatch):
    """The SAVE path must strip learned-owned triples instead of persisting
    them as explicit registration metadata (TTL/floor immunity bug)."""
    import lib.context_limits as limits
    from routes.config import _canonical_model_registrations

    monkeypatch.setattr(limits, '_LEARNED', {'sankuai::kimi-k3': 383_727})
    monkeypatch.setattr(limits, '_META', {
        'sankuai::kimi-k3': {'source': 'expand', 'ts': 0, 'strikes': 0},
    })
    providers = [{'id': 'sankuai', 'models': [
        {'model_id': 'kimi-k3', 'context_window': 383_727,
         'context_window_source': 'learned:sankuai',
         'context_window_exact': False},
        {'model_id': 'anonymous-model'},
    ]}]
    saved = _canonical_model_registrations(
        providers, activate=False, project_context=False)
    for row in saved[0]['models']:
        assert row['context_window'] is None
        assert row['context_window_source'] == 'unknown'
        assert row['context_window_exact'] is False
    # Display path still projects for the browser — and the projection is
    # the verified 1M, not the stale expand.
    shown = _canonical_model_registrations(providers, activate=False)
    assert shown[0]['models'][0]['context_window'] == 1_000_000
    assert shown[0]['models'][0]['context_window_source'] == 'repository_verified'


def test_learned_override_is_provider_model_scoped(monkeypatch):
    import lib.context_limits as limits
    from lib.model_info import resolved_context_profile

    monkeypatch.setattr(limits, '_LEARNED', {'provider-a::same-model': 321_000})
    monkeypatch.setattr(limits, '_META', {
        'provider-a::same-model': {'source': 'shrink', 'ts': 0},
    })
    assert resolved_context_profile('same-model', 'provider-a')['window'] == 321_000
    assert resolved_context_profile('same-model', 'provider-b')['window'] is None
    assert resolved_context_profile('same-model', 'provider-a')['source'] == 'learned:provider-a'


def test_capabilities_keeps_same_model_per_provider(monkeypatch):
    import lib
    from routes.api_v1.capabilities import _models_summary

    monkeypatch.setattr(lib, '_SAVED_CONFIG', {'providers': [
        {'id': 'a', 'models': [{'model_id': 'shared'}]},
        {'id': 'b', 'models': [{'model_id': 'shared'}]},
    ]})
    rows = _models_summary()
    assert len(rows) == 1
    assert rows[0]['id'] == 'shared'
    assert set(rows[0]['providers']) == {'a', 'b'}
    assert {
        offering['provider'] for offering in rows[0]['offerings']
    } == {'a', 'b'}
    assert all(offering['context'] == {
        'window': None, 'source': 'unknown', 'exact': False,
    } for offering in rows[0]['offerings'])


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_frontend_unknown_and_estimate_semantics(tmp_path):
    harness = tmp_path / 'context_profile_frontend.js'
    harness.write_text(r"""
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.document = {
  readyState: 'loading',
  addEventListener: () => {},
  getElementById: () => null,
};
global.getConvById = () => ({
  id: 'c1', model: global.currentModel, provider_id: 'p',
  messages: [{role: 'assistant', usage: {prompt_tokens: 50000}}],
});
global.activeConvId = 'c1';
global.currentModel = 'unknown-model';
global._contextPolicy = {per_model: {
  'p::estimated-model': {window: 100000, source: 'family_estimate', exact: false},
}};
global.runtimeScope.ConversationTurnRead = {
  state: () => ({liveRoundUsageByTurn: {}}),
  ordered: () => [{
    turnId: 'turn-1', actor: 'assistant', updatedAt: 1,
    projection: {lastRoundUsage: {tokensIn: 50000}},
  }],
};
const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('/* ===== migrated source: context-bar.js ===== */');
const end = source.indexOf('/* ===== migrated source: presence.js ===== */', start);
if (start < 0 || end < 0) throw new Error('context-bar migrated source not found');
eval(source.slice(start, end));
const unknown = contextUsageSummary();
global.currentModel = 'estimated-model';
const estimate = contextUsageSummary();
console.log(JSON.stringify({unknown, estimate}));
""", encoding='utf-8')
    proc = subprocess.run(
        ['node', str(harness),
         str(Path('frontend/src/runtime/app-runtime.js').resolve())],
        text=True, capture_output=True, check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload['unknown']['limit'] is None
    assert payload['unknown']['pct'] is None
    assert payload['unknown']['zone'] == 'unknown'
    assert payload['estimate']['pct'] == 50
    assert payload['estimate']['zone'] == 'ok'
    assert payload['estimate']['exact'] is False
    assert payload['estimate']['source'] == 'family_estimate'


def test_per_model_policy_has_bare_aliases():
    """routes.config._build_per_model_context_policy must emit BOTH the
    scoped ``provider::model`` key and the bare ``model_id`` alias.

    The frontend ``_resolveContextProfile`` contract tries the scoped key
    first, then the bare one — but the server historically wrote ONLY
    scoped keys, so the documented fallback could never hit. Legacy
    conversations (settings.provider_id fleet-wide NULL, verified
    2026-08-20) live and die by the bare alias."""
    from routes.config import _build_per_model_context_policy

    per_model = _build_per_model_context_policy([
        {'provider_id': 'sankuai', 'model_id': 'kimi-k3'},
        {'provider_id': 'other', 'model_id': 'kimi-k3'},   # name collision
        {'provider_id': '', 'model_id': ''},               # garbage row
        {'provider_id': 'sankuai', 'model_id': 'glm-5.3'},
    ])
    assert per_model['sankuai::kimi-k3']['window'] == 1_000_000
    # Bare alias exists and FIRST registration wins on collision.
    assert per_model['kimi-k3'] == per_model['sankuai::kimi-k3']
    assert per_model['other::kimi-k3']['window'] == 1_000_000
    assert per_model['glm-5.3'] == per_model['sankuai::glm-5.3']
    # The garbage row produced no keys.
    assert '' not in per_model and '::' not in per_model
    # Every scoped key has a bare twin.
    scoped = [k for k in per_model if '::' in k]
    for k in scoped:
        assert k.split('::', 1)[1] in per_model


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_frontend_providerless_conv_resolves_via_bare_key(tmp_path):
    """The 2026-08-20 fleet bug end-to-end at the JS layer: a conversation
    with NO provider_id (and a config without one) must still resolve its
    window via the bare model alias in per_model. A NEUTER variant (alias
    dropped) must fall back to zone 'unknown' — proving the alias is the
    load-bearing link."""
    harness = tmp_path / 'context_profile_providerless.js'
    harness.write_text(r"""
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.document = {
  readyState: 'loading',
  addEventListener: () => {},
  getElementById: () => null,
};
// Legacy conv: provider_id NEVER persisted; global config carries none either.
global.getConvById = () => ({
  id: 'c1', model: 'kimi-k3',
  messages: [{role: 'assistant', usage: {prompt_tokens: 50000}}],
});
global.activeConvId = 'c1';
const perModel = {'sankuai::kimi-k3': {window: 1000000, source: 'repository_verified', exact: true}};
if (process.env.WITH_BARE_ALIAS === '1') {
  perModel['kimi-k3'] = perModel['sankuai::kimi-k3'];
}
global._contextPolicy = {per_model: perModel};
global.runtimeScope.ConversationTurnRead = {
  state: () => ({liveRoundUsageByTurn: {}}),
  ordered: () => [{
    turnId: 'turn-1', actor: 'assistant', updatedAt: 1,
    projection: {lastRoundUsage: {tokensIn: 50000}},
  }],
};
const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('/* ===== migrated source: context-bar.js ===== */');
const end = source.indexOf('/* ===== migrated source: presence.js ===== */', start);
if (start < 0 || end < 0) throw new Error('context-bar migrated source not found');
eval(source.slice(start, end));
console.log(JSON.stringify(contextUsageSummary()));
""", encoding='utf-8')
    runtime = str(Path('frontend/src/runtime/app-runtime.js').resolve())

    import os
    env_with = dict(os.environ, WITH_BARE_ALIAS='1')
    proc = subprocess.run(['node', str(harness), runtime],
                          text=True, capture_output=True, check=True,
                          env=env_with)
    resolved = json.loads(proc.stdout)
    assert resolved['limit'] == 1_000_000
    assert resolved['zone'] == 'ok'
    assert resolved['pct'] == 5

    # NEUTER: without the bare alias the SAME conv is blind — the exact
    # production shape before the fix.
    proc = subprocess.run(['node', str(harness), runtime],
                          text=True, capture_output=True, check=True)
    blind = json.loads(proc.stdout)
    assert blind['limit'] is None
    assert blind['zone'] == 'unknown'
