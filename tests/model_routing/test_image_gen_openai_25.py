#!/usr/bin/env python3
"""GPT-Image-2.5 Sunburst registration and wire-shape contract tests.

The model was registered through the maintained exact-knowledge path
(openai.json recipe + _release.py + MODEL_PRICING + recipe-declared
``reasoning: unsupported``), so a fresh world-catalog insert must pass
``registration_missing_fields`` with zero issues — unlike the legacy
gpt-image rows, which predate declared-reasoning admission and survive as
unmarked legacy rows.

Wire shape (developers.openai.com/api/docs/models/gpt-image-2.5-sunburst,
checked 2026-09-20): the 2.5 family speaks the OpenAI sync images API,
accepts custom WIDTHxHEIGHT sizes (16-aligned edges, ≤3840px, ≤4K total),
and adds xhigh/max quality rungs. The 2K resolution hint maps to
~2048px-class custom dimensions for the 2.5 family only; legacy models keep
their 1024-class behavior.

No network. Run standalone or via pytest.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ── wire dialect + size mapping ─────────────────────────────────────

def test_sunburst_is_openai_image_model():
    from lib.image_gen import _is_openai_model
    assert _is_openai_model('gpt-image-2.5-sunburst')
    _ok('gpt-image-2.5-sunburst uses the OpenAI sync images API')


def test_size_mapping_2k_only_for_25_family():
    from lib.image_gen import _size_for_openai
    # 2K hint on the 2.5 family → documented custom 2048px-class dimensions.
    assert _size_for_openai('gpt-image-2.5-sunburst', '1:1', '2K') == '2048x2048'
    assert _size_for_openai('gpt-image-2.5-sunburst', '16:9', '2K') == '2560x1440'
    assert _size_for_openai('gpt-image-2.5-sunburst', '9:16', '2K') == '1440x2560'
    # Custom sizes stay 16-aligned and inside the documented 4K envelope.
    for ar in ('1:1', '16:9', '9:16', '4:3', '3:4'):
        w, h = (int(x) for x in _size_for_openai(
            'gpt-image-2.5-sunburst', ar, '2K').split('x'))
        assert w % 16 == 0 and h % 16 == 0
        assert max(w, h) <= 3840 and 655360 <= w * h <= 8294400
    # 1K / unset hints keep the legacy recommended sizes, even on 2.5.
    assert _size_for_openai('gpt-image-2.5-sunburst', '1:1', '1K') == '1024x1024'
    assert _size_for_openai('gpt-image-2.5-sunburst', '16:9', '') == '1536x1024'
    # Legacy models never get the 2K upgrade (established cost/latency).
    assert _size_for_openai('gpt-image-2', '1:1', '2K') == '1024x1024'
    assert _size_for_openai('dall-e-3', '1:1', '2K') == '1024x1024'
    _ok('2K custom sizes gate to the GPT-Image-2.5 family')


def test_sunburst_excluded_from_chat_pickers():
    from lib.model_info.capability_taxonomy import is_chat_model
    assert not is_chat_model(['image_gen'])
    _ok('image_gen capability stays out of chat pickers')


# ── registration admission (maintained exact knowledge) ─────────────

def _compiled_sunburst_bundle():
    from lib.provider.provider_template_recipes import (
        compile_provider_template_bundle, load_provider_templates)
    from lib.model_routing.model_defaults import official_model_directory
    templates = load_provider_templates()
    directory = official_model_directory(templates)
    bundle = compile_provider_template_bundle(
        'openai', official_directory=directory, _templates=templates)
    for row in bundle['models']:
        if row['model_id'] == 'gpt-image-2.5-sunburst':
            return bundle, row
    raise AssertionError('gpt-image-2.5-sunburst missing from the openai bundle')


def test_sunburst_registration_facts_complete():
    from lib.model_routing.domain import normalize_document, public_projection
    from lib.model_routing.registration import registration_missing_fields
    bundle, row = _compiled_sunburst_bundle()
    assert row['creator_id'] == 'openai'
    assert row['capabilities'] == ['image_gen']
    doc = normalize_document({
        'contract_version': 'tofu.model-routing/v2', 'revision': 0,
        'creators': bundle['creators'],
        'models': [row], 'providers': [], 'provider_accesses': [],
        'connections': [], 'credentials': [], 'offerings': [], 'deployments': [],
    })
    projected = public_projection(doc)['models'][0]
    assert projected['release_date'] == '2026-09-08'
    pricing = projected.get('list_pricing') or projected.get('official_pricing')
    assert pricing and pricing['currency'] == 'USD' and pricing['input'] > 0 \
        and pricing['output'] > 0
    assert projected['inference_profile']['reasoning']['support'] == 'unsupported'
    missing = registration_missing_fields(projected)
    assert missing == [], f'sunburst admission gaps: {missing}'
    _ok('sunburst passes registration admission with zero missing facts')


if __name__ == '__main__':
    test_sunburst_is_openai_image_model()
    test_size_mapping_2k_only_for_25_family()
    test_sunburst_excluded_from_chat_pickers()
    test_sunburst_registration_facts_complete()
    print(_color('\nAll GPT-Image-2.5 contract checks passed.', '32'))
