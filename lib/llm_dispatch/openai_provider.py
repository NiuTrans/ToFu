"""Canonicalize the official OpenAI provider onto the GPT-5.6 contract.

Only ``api.openai.com`` is rewritten. OpenAI-compatible gateways may expose a
different protocol or model catalogue and must keep their explicit settings.
"""

from __future__ import annotations

import copy
from urllib.parse import urlparse

from lib.log import get_logger


logger = get_logger(__name__)


def is_official_openai_provider(provider: dict) -> bool:
    if not isinstance(provider, dict) or provider.get('oauth') == 'codex':
        return False
    raw_url = str(provider.get('base_url') or '').strip()
    if not raw_url:
        return False
    try:
        return (urlparse(raw_url).hostname or '').lower() == 'api.openai.com'
    except ValueError as exc:
        logger.debug('[OpenAIProvider] invalid base URL: %s', exc)
        return False


def normalize_official_openai_provider(provider: dict) -> dict:
    """Return a copy with current official transport and GPT-5.6 model IDs.

    ``gpt-5.6-pro`` was never a public model ID. Existing local configs are
    migrated to the ``gpt-5.6`` alias; Pro is expressed by
    ``reasoning.mode='pro'`` on a normal GPT-5.6 request.
    """
    normalized = copy.deepcopy(provider) if isinstance(provider, dict) else {}
    if not is_official_openai_provider(normalized):
        return normalized

    normalized['protocol'] = 'responses'
    normalized['responses_profile'] = 'openai'
    models = normalized.get('models')
    if not isinstance(models, list):
        return normalized

    result = []
    seen = set()
    for raw_model in models:
        if not isinstance(raw_model, dict):
            continue
        model = copy.deepcopy(raw_model)
        if model.get('model_id') == 'gpt-5.6-pro':
            model['model_id'] = 'gpt-5.6'
        aliases = model.get('aliases')
        if isinstance(aliases, list):
            model['aliases'] = [
                'gpt-5.6' if value == 'gpt-5.6-pro' else value
                for value in aliases
            ]
        model_id = model.get('model_id')
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        seen.add(model_id)
        result.append(model)
    normalized['models'] = result
    return normalized


def normalize_official_openai_providers(providers: list) -> list:
    if not isinstance(providers, list):
        return []
    return [normalize_official_openai_provider(provider)
            for provider in providers if isinstance(provider, dict)]
