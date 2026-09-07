"""Verified GPT-5.6 public-API contract shared by backend consumers.

The declarative source is package data in ``lib/model_info/data/openai.json``.
The setup UI, bootstrap flow, pricing, routing, and model capability code all
consume that same file, so the published wheel does not depend on a checkout's
frontend ``static/`` tree. This module is deliberately stdlib-only and safe to
import from model hot paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_CONTRACT_PATH = Path(__file__).with_name('data') / 'openai.json'


def _load_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    payload = json.loads(_CONTRACT_PATH.read_text(encoding='utf-8'))
    contract = payload.get('contract')
    models = contract.get('models') if isinstance(contract, dict) else None
    if not isinstance(models, dict) or not models:
        raise RuntimeError(f'invalid GPT-5.6 contract: {_CONTRACT_PATH}')
    offerings = payload.get('offering_recipes')
    if offerings is None:  # Legacy checked-out templates remain readable.
        offerings = payload.get('models')
    template_ids = {
        str(item.get('model_id') or '')
        for item in offerings or () if isinstance(item, dict)
    }
    missing = set(models) - template_ids
    if missing:
        raise RuntimeError(
            'GPT-5.6 contract models missing from OpenAI template: '
            + ', '.join(sorted(missing)))
    return payload, contract


OPENAI_TEMPLATE, GPT56_CONTRACT = _load_contract()
GPT56_MODEL_IDS = frozenset(GPT56_CONTRACT['models'])
GPT56_ALIAS_TARGET = str(GPT56_CONTRACT['alias']['gpt-5.6'])
GPT56_REASONING_EFFORTS = tuple(GPT56_CONTRACT['reasoning_efforts'])
GPT56_DEFAULT_REASONING_EFFORT = str(
    GPT56_CONTRACT['default_reasoning_effort'])
GPT56_CONTEXT_WINDOW = int(GPT56_CONTRACT['context_window'])
GPT56_MAX_OUTPUT_TOKENS = int(GPT56_CONTRACT['max_output_tokens'])
GPT56_LONG_CONTEXT_THRESHOLD = int(
    GPT56_CONTRACT['long_context_threshold'])
GPT56_CACHE_WRITE_MUL = float(GPT56_CONTRACT['cache_write_mul'])
GPT56_CACHE_READ_MUL = float(GPT56_CONTRACT['cache_read_mul'])


def is_official_gpt56_model(model: str) -> bool:
    return str(model or '').strip().lower() in GPT56_MODEL_IDS


def normalize_gpt56_reasoning_effort(value: Any) -> str:
    """Map Tofu's depth vocabulary onto the public GPT-5.6 enum."""
    requested = str(value or GPT56_DEFAULT_REASONING_EFFORT).strip().lower()
    aliases = {'off': 'none', 'minimal': 'none', 'ultra': 'max'}
    normalized = aliases.get(requested, requested)
    return (normalized if normalized in GPT56_REASONING_EFFORTS
            else GPT56_DEFAULT_REASONING_EFFORT)


def gpt56_pricing_rows() -> dict[str, dict[str, Any]]:
    """Return official public prices, including the >272K full-request tier."""
    rows: dict[str, dict[str, Any]] = {}
    for model_id, spec in GPT56_CONTRACT['models'].items():
        input_price = float(spec['input'])
        output_price = float(spec['output'])
        tiers = [
            {
                'id': 'ctx_le_272000',
                'maxPromptTokens': GPT56_LONG_CONTEXT_THRESHOLD,
                'input': input_price,
                'output': output_price,
                'cacheWriteMul': GPT56_CACHE_WRITE_MUL,
                'cacheReadMul': GPT56_CACHE_READ_MUL,
            },
            {
                'id': 'ctx_gt_272000',
                'maxPromptTokens': GPT56_CONTEXT_WINDOW,
                'input': input_price * 2,
                'output': output_price * 1.5,
                'cacheWriteMul': GPT56_CACHE_WRITE_MUL,
                'cacheReadMul': GPT56_CACHE_READ_MUL,
            },
        ]
        rows[model_id] = {
            'input': input_price,
            'output': output_price,
            'cacheWriteMul': GPT56_CACHE_WRITE_MUL,
            'cacheReadMul': GPT56_CACHE_READ_MUL,
            'name': str(spec['name']),
            'contextTiers': tiers,
        }
    return rows


def gpt56_slot_configs() -> dict[str, dict[str, Any]]:
    """Return local router seeds without presenting estimates as API facts.

    Price/capability comes from the verified contract. RPM and latency are
    local selection heuristics: OpenAI rate limits are account-specific and
    the public API does not promise a universal per-model latency.
    """
    rows: dict[str, dict[str, Any]] = {}
    offerings = OPENAI_TEMPLATE.get('offering_recipes')
    if offerings is None:  # Compatibility with pre-recipe source trees.
        offerings = OPENAI_TEMPLATE.get('models')
    templates = {
        str(item.get('model_id') or ''): item
        for item in offerings or ()
        if isinstance(item, dict)
    }
    local_latency = {
        'flagship': 5000,
        'balanced': 3000,
        'high_volume': 1800,
    }
    for model_id, spec in GPT56_CONTRACT['models'].items():
        caps = {'text', 'vision', 'thinking'}
        role = str(spec.get('role') or 'flagship')
        if role in {'balanced', 'high_volume'}:
            caps.add('cheap')
        template = templates.get(model_id, {})
        rows[model_id] = {
            'caps': caps,
            'rpm': int(template.get('rpm') or 30),
            'latency': local_latency.get(role, 5000),
            'cost': float(spec['output']) / 1000,
        }
    return rows


__all__ = [
    'OPENAI_TEMPLATE', 'GPT56_CONTRACT', 'GPT56_MODEL_IDS',
    'GPT56_ALIAS_TARGET', 'GPT56_REASONING_EFFORTS',
    'GPT56_DEFAULT_REASONING_EFFORT', 'GPT56_CONTEXT_WINDOW',
    'GPT56_MAX_OUTPUT_TOKENS', 'GPT56_LONG_CONTEXT_THRESHOLD',
    'GPT56_CACHE_WRITE_MUL', 'GPT56_CACHE_READ_MUL',
    'is_official_gpt56_model', 'normalize_gpt56_reasoning_effort',
    'gpt56_pricing_rows', 'gpt56_slot_configs',
]
