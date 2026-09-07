"""Executable spec for creator identity attribution.

Relay providers (Bedrock, Azure, Vertex, NIM, DashScope) re-publish other
creators' models under decorated ids. These pins fix the stripping,
attribution and dedupe-key rules the directory and the catalog cleanup rely
on.
"""

from __future__ import annotations

import pytest

from lib.model_catalog._creator_identity import (
    brand_family,
    canonical_key,
    creator_family,
    has_regional_prefix,
    is_creator_row,
    strip_routing_decoration,
)


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('raw,expected', [
    # Bedrock regional + namespace + revision markers.
    ('au.anthropic.claude-opus-4-6-v1', 'claude-opus-4-6'),
    ('global.anthropic.claude-opus-5', 'claude-opus-5'),
    ('us.anthropic.claude-haiku-4-5-20251001-v1:0',
     'claude-haiku-4-5-20251001'),
    ('anthropic.claude-opus-4-1-20250805-v1:0', 'claude-opus-4-1-20250805'),
    ('openai.gpt-5.6-sol', 'gpt-5.6-sol'),
    ('amazon.nova-pro-v1:0', 'nova-pro'),
    ('xai.grok-4.6', 'grok-4.6'),
    # Publisher path segments (NIM / DashScope style).
    ('deepseek-ai/DeepSeek-V3', 'DeepSeek-V3'),
    ('meta/llama-3-8b-instruct', 'llama-3-8b-instruct'),
    ('qwen/qwen3-235b-a22b', 'qwen3-235b-a22b'),
    ('google/gemma-2-2b-it', 'gemma-2-2b-it'),

    ('zai.glm-5', 'glm-5'),
    ('zai-org/glm-5-maas', 'glm-5'),
    ('stepfun-ai/step-3.7-flash', 'step-3.7-flash'),
    ('minimaxai/minimax-m2.7', 'minimax-m2.7'),
    ('meta/muse-glimmer-30b', 'muse-glimmer-30b'),
    ('mistralai/ministral-14b-instruct-2512',
     'ministral-14b-instruct-2512'),
    # Vertex snapshot markers.
    ('claude-opus-4-1@20250805', 'claude-opus-4-1'),
    ('gemini-2.5-pro@default', 'gemini-2.5-pro'),
    # First-party ids pass through untouched — including bare ``-vN`` model
    # names that only strip once relay decoration was seen.
    ('deepseek-v3', 'deepseek-v3'),
    ('deepseek-v3-0324', 'deepseek-v3-0324'),
    ('qwen2.5-7b-instruct', 'qwen2.5-7b-instruct'),
    ('gpt-4o', 'gpt-4o'),
    ('claude-haiku-4-5-20251001', 'claude-haiku-4-5-20251001'),
    ('glm-4.5v', 'glm-4.5v'),
    ('', ''),
    (None, ''),
])
def test_strip_routing_decoration(raw, expected):
    assert strip_routing_decoration(raw) == expected


@pytest.mark.parametrize('raw,expected', [
    ('us.anthropic.claude-opus-5', True),
    ('global.openai.gpt-5.6-sol', True),
    ('anthropic.claude-opus-5', False),
    ('claude-opus-5', False),
    ('gpt-4o', False),
])
def test_has_regional_prefix(raw, expected):
    assert has_regional_prefix(raw) is expected


def test_canonical_key_unifies_creator_and_relay_skus():
    expected = canonical_key('claude-haiku-4-5')
    assert canonical_key('claude-haiku-4-5-20251001') == expected
    assert canonical_key('anthropic.claude-haiku-4-5-20251001-v1:0') == expected
    assert canonical_key('global.anthropic.claude-haiku-4-5') == expected
    assert canonical_key('claude-haiku-4-5@20251001') == expected

    gpt4o = canonical_key('gpt-4o')
    assert canonical_key('gpt-4o-2024-08-06') == gpt4o
    assert canonical_key('openai.gpt-4o') == gpt4o


    # Publisher namespaces and relay-only channel suffixes are not public
    # model identity. The creator spelling, Z.AI/Model-as-a-Service spellings,
    # and a provider-qualified spelling collapse onto one logical key.
    assert canonical_key('zai.glm-5') == canonical_key('glm-5')
    assert canonical_key('zai-org/glm-5-maas') == canonical_key('glm-5')
    assert canonical_key('stepfun-ai/step-3.7-flash') == canonical_key(
        'step-3.7-flash')
    assert canonical_key('deepseek-v3.2-maas') == canonical_key(
        'deepseek-v3.2')
    assert canonical_key('meta/muse-glimmer-30b') == canonical_key(
        'muse-glimmer-30b')
    # Six-digit version months distinguish models and must survive.
    assert canonical_key('deepseek-v3') != canonical_key('deepseek-v3-0324')
    assert canonical_key('kimi-k2-0711') != canonical_key('kimi-k2')


@pytest.mark.parametrize('raw,expected', [
    ('anthropic.claude-opus-5', 'anthropic'),
    ('global.anthropic.claude-opus-5', 'anthropic'),
    ('openai.gpt-5.6-sol', 'openai'),
    ('gpt-4o-2024-08-06', 'openai'),

    ('codex-mini', 'openai'),
    ('text-embedding-3-large', 'openai'),
    ('deep-research-preview-04-2026', 'openai'),
    ('xai.grok-4.6', 'xai'),
    ('amazon.nova-pro-v1:0', 'amazon'),
    ('deepseek-ai/DeepSeek-V3', 'deepseek'),
    ('qwen/qwen3-235b-a22b', 'alibaba'),

    ('qvq-max', 'alibaba'),
    ('meta/llama-3-8b-instruct', 'meta'),

    ('meta/muse-glimmer-30b', 'meta'),

    ('meta/esmfold', 'meta'),
    ('nvidia/usdcode', 'nvidia'),
    ('openai/whisper-large-v3', 'openai'),
    ('upstage/solar-10.7b-instruct', 'upstage'),
    ('deepseek.r1-v1:0', 'deepseek'),
    ('us.deepseek.r1-v1:0', 'deepseek'),
    ('muse-spark-1.2', 'meta'),
    ('muse-spark-1.2-contributor', 'meta'),
    ('zai.glm-5', 'zhipu'),
    ('zai-org/glm-5-maas', 'zhipu'),
    ('stepfun-ai/step-3.7-flash', 'stepfun'),
    ('minimaxai/minimax-m2.7', 'minimax'),
    ('mistralai/ministral-14b-instruct-2512', 'mistral'),

    ('labs-devstral-small-2512', 'mistral'),
    ('lyria-3-pro-preview', 'google'),
    ('google/gemma-2-2b-it', 'google'),
    ('phi-4', 'microsoft'),
    ('minimax-m2.5', 'minimax'),
    ('kimi-k2-0711', 'moonshot'),
    ('glm-4.6', 'zhipu'),
    ('doubao-seed-1-6', 'bytedance'),
    ('Ling-1T', 'bailing'),
    # ink**ling** contains 'ling' but is Thinking Machines, not Ant Group.
    ('thinkingmachines/inkling', 'thinkingmachines'),
    ('inkling', 'thinkingmachines'),
    ('some-unrelated-model', None),
    ('', None),
])
def test_creator_family(raw, expected):
    assert creator_family(raw) == expected


@pytest.mark.parametrize('brand,expected', [
    ('amazon', 'amazon'),
    ('Microsoft', 'microsoft'),
    ('anthropic', 'anthropic'),
    ('bailing', 'bailing'),
    ('inkling', 'thinkingmachines'),
    ('Thinking Machines', 'thinkingmachines'),
    ('oauth', None),
    ('', None),
    (None, None),
])
def test_brand_family(brand, expected):
    assert brand_family(brand) == expected


@pytest.mark.parametrize('provider,family,expected', [
    ('anthropic', 'anthropic', True),
    ('amazon-bedrock', 'anthropic', False),
    ('amazon-bedrock', 'amazon', True),
    ('nova', 'amazon', True),
    ('azure', 'openai', True),
    ('azure', 'microsoft', True),
    ('azure', 'anthropic', False),
    ('google-vertex', 'google', True),
    ('google-vertex', 'anthropic', False),
    ('openai', None, False),
])
def test_is_creator_row(provider, family, expected):
    assert is_creator_row(provider, family) is expected
