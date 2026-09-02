#!/usr/bin/env python3
"""Unit tests for lib/model_info/_folds.py — picker display-fold SSOT.

WHY
---
Gateway endpoints (Meituan et al.) expose dozens of near-identical rows:
cloud mirrors of one deployment, and long version series of one line. The
picker folds them — display-only — using the metadata this module emits. A
wrong fold here silently hides a model the user configured, so both the
fold rule AND the anti-fold guards (capability signature, provider scope,
generation split) are pinned.

WHAT IS GUARDED (results, not implementation)
---------------------------------------------
  * family_key strips version tokens but never merges generations
    (deepseek-v3.2 ≠ deepseek-v4-pro) or letter-bearing SKUs (qwen3.5-plus).
  * Alias fold: routing-pool containment and MODEL_ALIAS_GROUPS membership
    both fold; canonical prefers the explicit-pool logical entry.
  * The capability/thinking signature guard keeps a DIFFERENTLY-configured
    mirror unfolded.
  * Folds never cross provider scope.
  * Family primary = explicit `recommended` flag > highest version tuple.
  * Singletons and families of one emit NO metadata (picker stays flat).

NEUTER: remove the signature guard (fold regardless of capabilities) →
test_alias_fold_capability_guard loses its unfolded assertion (red).
"""

from __future__ import annotations

import pytest

from lib.model_info._folds import (
    build_fold_index,
    family_key,
    version_tuple,
)

pytestmark = pytest.mark.unit


def entry(mid, scope='prov', caps=('text',), routing=(), pool=False,
          thinking=False, recommended=False):
    return {
        'scope': scope,
        'model_id': mid,
        'capabilities': list(caps),
        'thinking_default': thinking,
        'routing': set(routing) | {mid},
        'explicit_pool': pool,
        'recommended': recommended,
    }


# ── family_key ───────────────────────────────────────────────────────

class TestFamilyKey:
    @pytest.mark.parametrize('mid,fam', [
        ('glm-5.1', 'glm'),
        ('glm-5.3', 'glm'),
        ('GLM-5.2-Air', 'glm-air'),
        ('claude-opus-4.8', 'claude-opus'),
        ('aws.claude-opus-4.8', 'claude-opus'),
        ('us.anthropic.claude-opus-4-8-v1:0', 'claude-opus'),
        ('MiniMax-M2.5', 'minimax'),
        ('MiniMax-M3', 'minimax'),
        ('kimi-k3', 'kimi'),
        ('qwen3.5-plus', 'qwen3-plus'),
        ('gemini-3.5-flash-lite', 'gemini-flash-lite'),
        # trailing stage marker is not a line discriminator (2026-08-23):
        ('gemini-3-flash-preview', 'gemini-flash'),
        ('gemini-3.5-flash', 'gemini-flash'),
        ('gemini-3.1-pro-preview', 'gemini-pro'),
        ('gemini-3.1-flash-image-preview', 'gemini-flash-image'),
        ('LongCat-Flash-Thinking-2601', 'longcat-flash-thinking'),
        ('gpt-4o', 'gpt'),
        ('gpt-5.6-sol', 'gpt-sol'),
        # generations NEVER merge:
        ('deepseek-v3.2', 'deepseek'),
        ('deepseek-v4-pro', 'deepseek-pro'),
        ('deepseek-v4-flash', 'deepseek-flash'),
        ('gpt-5.6-terra', 'gpt-terra'),
    ])
    def test_key(self, mid, fam):
        assert family_key(mid) == fam

    def test_pathological_all_version_tokens_falls_back(self):
        assert family_key('4-5-6') == '4-5-6'

    def test_empty(self):
        assert family_key('') == ''


class TestVersionTuple:
    def test_numeric_ordering(self):
        assert version_tuple('glm-5.10') > version_tuple('glm-5.9')

    def test_multi_run(self):
        assert version_tuple('claude-opus-4-8') == (4, 8)


# ── alias fold ───────────────────────────────────────────────────────

class TestAliasFold:
    def test_routing_pool_containment_folds(self):
        """Standalone mirror entry folds under the logical entry whose
        request_ids pool absorbs it (the pre-consolidation config shape)."""
        folds = build_fold_index([
            entry('deepseek-v3.2', pool=True,
                  routing={'deepseek-v3.2-tencent', 'deepseek-v3.2-baidu'}),
            entry('deepseek-v3.2-baidu'),
        ])
        assert folds['prov::deepseek-v3.2']['fold_canonical'] == 'deepseek-v3.2'
        assert (folds['prov::deepseek-v3.2-baidu']['fold_group']
                == 'prov:deepseek-v3.2')

    def test_static_alias_group_folds_without_pools(self):
        """Two standalone ids known interchangeable by MODEL_ALIAS_GROUPS."""
        alias_map = {
            'glm-5.1': {'glm-5.1', 'glm-5.1-huawei'},
            'glm-5.1-huawei': {'glm-5.1', 'glm-5.1-huawei'},
        }
        folds = build_fold_index(
            [entry('glm-5.1'), entry('glm-5.1-huawei')], alias_map)
        assert (folds['prov::glm-5.1-huawei']['fold_canonical'] == 'glm-5.1')

    def test_alias_fold_capability_guard(self):
        """A mirror configured with DIFFERENT capabilities is its own row."""
        folds = build_fold_index([
            entry('m', pool=True, routing={'m', 'm-mirror'}),
            entry('m-mirror', caps=('text', 'vision')),
        ])
        assert 'prov::m-mirror' not in folds
        assert 'prov::m' not in folds

    def test_alias_fold_never_crosses_scope(self):
        folds = build_fold_index([
            entry('glm-5.1', scope='a'),
            entry('glm-5.1-huawei', scope='b'),
        ], {'glm-5.1': {'glm-5.1-huawei'}, 'glm-5.1-huawei': {'glm-5.1'}})
        assert folds == {}

    def test_singleton_no_metadata(self):
        assert build_fold_index([entry('solo-model')]) == {}


# ── family fold ──────────────────────────────────────────────────────

class TestFamilyFold:
    def test_highest_version_is_primary(self):
        folds = build_fold_index([
            entry('glm-5.1'), entry('glm-5.2'), entry('glm-5.3'),
        ])
        for mid in ('glm-5.1', 'glm-5.2', 'glm-5.3'):
            assert folds[f'prov::{mid}']['family_primary'] == 'glm-5.3'

    def test_explicit_recommended_beats_version(self):
        folds = build_fold_index([
            entry('glm-5.2'), entry('glm-5.3', recommended=True),
            entry('my-glm-custom', recommended=True),
        ])
        # Two explicit flags → version tiebreak inside the explicit pool
        # (both have no digits → equal tuples → alphabetical-first).
        folds2 = build_fold_index([
            entry('glm-5.3'), entry('glm-5.2', recommended=True),
        ])
        assert folds2['prov::glm-5.3']['family_primary'] == 'glm-5.2'
        assert folds  # sanity: first call produced something

    def test_preview_tail_folds_with_ga_line(self):
        """gemini-3-flash-preview must join the gemini-flash family — the
        '-preview' stage suffix previously stranded it as its own family."""
        folds = build_fold_index([
            entry('gemini-3-flash-preview'), entry('gemini-3.5-flash'),
            entry('gemini-3.6-flash'),
        ])
        fam = folds['prov::gemini-3-flash-preview']['family']
        assert fam == folds['prov::gemini-3.5-flash']['family']
        assert folds['prov::gemini-3-flash-preview']['family_primary'] == 'gemini-3.6-flash'
    def test_single_member_family_not_folded(self):
        folds = build_fold_index([entry('deepseek-v4-pro')])
        assert 'prov::deepseek-v4-pro' not in folds

    def test_family_scoped_per_provider(self):
        folds = build_fold_index([
            entry('glm-5.1', scope='a'), entry('glm-5.3', scope='b'),
        ])
        assert folds == {}

    def test_alias_mirrors_do_not_family_fold_separately(self):
        """Folded-away mirrors stay out of the family pass — the alias unit
        folds as a whole via its canonical entry."""
        folds = build_fold_index([
            entry('glm-5.1', pool=True, routing={'glm-5.1', 'glm-5.1-huawei'}),
            entry('glm-5.1-huawei'),
            entry('glm-5.3'),
        ])
        # mirror carries alias metadata only, no family of its own
        assert 'family' not in folds['prov::glm-5.1-huawei']
        # canonical + newer version form one family
        assert folds['prov::glm-5.1']['family_primary'] == 'glm-5.3'


# ── NEUTER self-proof ────────────────────────────────────────────────

class TestNeuterProof:
    def test_signature_guard_bites(self):
        """If the signature guard were removed, the differently-configured
        mirror WOULD fold — proving the guard above is load-bearing."""
        entries = [
            entry('m', pool=True, routing={'m', 'm-mirror'}),
            entry('m-mirror', caps=('text', 'vision')),
        ]
        folds = build_fold_index(entries)
        assert folds == {}
        # neutered: drop the guard → same inputs fold
        import lib.model_info._folds as folds_mod
        original = folds_mod._signature
        folds_mod._signature = lambda e: ('x',)
        try:
            assert build_fold_index(entries) != {}
        finally:
            folds_mod._signature = original


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
