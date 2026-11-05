"""Kimi K3.1 Pro: exact first-party registration and its served wires.

Facts checked 2026-10-27 (docs/MODEL_REGISTRATION.md, "Kimi K3.1 Pro"): the
official quickstart fixes the always-thinking ``reasoning_effort`` ladder
(low/high/max, default max, no disable rung) and the fixed sampling
parameters; Moonshot's pricing page fixes $2/$0.20/$10 per million tokens and
the 1,048,576-token window. The ``kimi-k3`` release and context needles also
match ``kimi-k3.1-pro``, so these specs pin that K3.1 does not inherit K3's
facts.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

MODEL = {'creator_id': 'moonshot', 'model_id': 'kimi-k3.1-pro'}
FIXED_SAMPLING = {'temperature', 'top_p', 'presence_penalty', 'frequency_penalty'}


def _bundle(key):
    from lib.model_routing.model_defaults import official_model_directory
    from lib.provider.provider_template_recipes import (
        compile_provider_template_bundle, load_provider_templates,
    )
    templates = load_provider_templates()
    return compile_provider_template_bundle(
        key, official_directory=official_model_directory(templates),
        _templates=templates)


def _wire_ids(bundle):
    offerings = {o['offering_id'] for o in bundle['offerings']
                 if o.get('model') == MODEL}
    return [d['wire_model_id'] for d in bundle['deployments']
            if d['offering_id'] in offerings]


def _public_model(bundle):
    from lib.model_routing.domain import normalize_document, public_projection
    row = next(m for m in bundle['models'] if m['model_id'] == MODEL['model_id'])
    document = normalize_document({
        'contract_version': 'tofu.model-routing/v2', 'revision': 0,
        'creators': bundle['creators'], 'models': [row], 'providers': [],
        'provider_accesses': [], 'connections': [], 'credentials': [],
        'offerings': [], 'deployments': [],
    })
    return public_projection(document)['models'][0]


def test_official_template_registers_complete_first_party_facts():
    from lib.model_routing.registration import registration_missing_fields

    bundle = _bundle('kimi')
    assert _wire_ids(bundle) == ['kimi-k3.1-pro']
    model = _public_model(bundle)
    assert model['release_date'] == '2026-10-23'
    assert model['context_window'] == 1_048_576
    assert {'text', 'vision', 'video', 'thinking'} <= set(model['capabilities'])
    price = model.get('list_pricing') or model.get('official_pricing')
    assert (price['input'], price['output']) == (2.0, 10.0)
    assert price['cache_read'] == pytest.approx(0.20)
    assert (price['currency'], price['unit']) == ('USD', 'per_million_tokens')
    reasoning = model['inference_profile']['reasoning']
    assert reasoning['wire_format'] == 'reasoning_effort'
    assert reasoning['can_disable'] is False
    assert reasoning['supported_efforts'] == ['low', 'high', 'max']
    assert reasoning['default_effort'] == 'max'
    assert FIXED_SAMPLING <= set(model['inference_profile']['sampling']['omit'])
    assert registration_missing_fields(model) == []


# Each relay's public /models catalog listed this id on 2026-10-27.
@pytest.mark.parametrize('relay', ['openrouter', 'novita', 'nanogpt'])
def test_relay_prefix_stays_on_the_provider_wire(relay):
    bundle = _bundle(relay)
    assert _wire_ids(bundle) == ['moonshotai/kimi-k3.1-pro']
    assert all(row['model_id'] != 'moonshotai/kimi-k3.1-pro'
               for row in bundle['models'])


# Live-verified on the example-corp gateway, both faces, 2026-10-27.
@pytest.mark.parametrize(('depth', 'effort'), [
    ('off', 'low'), ('low', 'low'), ('medium', 'high'), ('high', 'high'),
    ('xhigh', 'max'), ('max', 'max'), ('default', 'max'),
])
def test_shipped_profile_compiles_the_always_thinking_ladder(depth, effort):
    from lib.llm.body import build_body
    from lib.model_routing.domain import ModelRef
    from lib.model_routing.model_defaults import current_model_defaults

    profile, _previous = current_model_defaults().resolve(ModelRef.from_value(MODEL))
    body = build_body('kimi-k3.1-pro', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth=depth, temperature=0.6,
                      inference_profile=profile)
    assert body['reasoning_effort'] == effort
    assert 'thinking' not in body
    assert not FIXED_SAMPLING & body.keys()
