"""Registration and wire-contract guards for GLM-5.3."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'glm-5.3'
EXPECTED_CAPS = {'text', 'thinking'}


def test_glm53_is_in_official_and_meituan_templates():
    source = (ROOT / 'frontend/src/runtime/app-runtime.js').read_text(
        encoding='utf-8')
    assert 'migrated source: settings/provider_templates.js' in source
    assert "model_id: 'glm-5.3'" in source

    template = json.loads(
        (ROOT / 'static/provider_templates/meituan.json').read_text(
            encoding='utf-8'))
    entry = next(
        (row for row in template['models'] if row.get('model_id') == MODEL),
        None)
    assert entry is not None
    assert set(entry['capabilities']) == EXPECTED_CAPS
    assert entry['rpm'] == 60
    assert entry['context_window'] == 1_000_000
    assert entry['pricing']['input'] == pytest.approx(3.45)
    assert entry['pricing']['output'] == pytest.approx(13.81)
    assert 'cost' not in entry


def test_glm53_has_dispatch_defaults_and_family_wire_shape():
    from lib.llm import build_body
    from lib.llm_dispatch.config._slots import DEFAULT_SLOT_CONFIGS
    from lib.model_info import _clamp_max_tokens, context_profile, is_glm

    assert is_glm(MODEL)
    assert DEFAULT_SLOT_CONFIGS[MODEL]['caps'] == EXPECTED_CAPS

    body = build_body(
        MODEL, [{'role': 'user', 'content': 'hi'}],
        max_tokens=200_000, thinking_enabled=True, stream=False)
    assert body['model'] == MODEL
    assert body['thinking'] == {'type': 'enabled'}
    assert body['max_tokens'] == _clamp_max_tokens(MODEL, 200_000) == 131_072

    # The canonical model registration owns the context window; model-info
    # consumes that same declaration instead of maintaining another table.
    from lib.model_registration import register_model
    register_model({
        'model_id': MODEL,
        'capabilities': sorted(EXPECTED_CAPS),
        'context_window': 1_000_000,
        'pricing': {'input': 3.45, 'output': 13.81},
    })
    assert context_profile(MODEL) == {
        'window': 1_000_000,
        'source': 'model_registration',
        'exact': True,
    }


def test_name_discovery_infers_text_thinking_without_fake_vision():
    from lib.llm_dispatch.discovery import _infer_capabilities

    assert _infer_capabilities(MODEL) == EXPECTED_CAPS
