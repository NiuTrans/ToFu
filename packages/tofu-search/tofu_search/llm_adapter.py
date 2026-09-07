"""tofu_search.llm_adapter — LLM callable adapter for content filtering.

Provides a unified interface for calling LLMs:
  - Custom callable (user-provided function)
  - OpenAI-compatible HTTP API (api_key + base_url + model)

The adapter normalizes both into a simple: call(messages, **kwargs) -> str
"""


import requests

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['call_llm']


def call_llm(messages: list[dict], *, config=None, **kwargs) -> str:
    """Call an LLM using the configured method.

    Args:
        messages: OpenAI-format message list ([{role, content}, ...]).
        config: SearchConfig instance. If None, uses global config.
        **kwargs: Extra parameters passed to the LLM (stop, temperature,
                  max_tokens, etc.).

    Returns:
        The assistant's response text.

    Raises:
        RuntimeError: If no LLM is configured.
        Exception: Propagated from the LLM call.
    """
    if config is None:
        from tofu_search.config import get_config
        config = get_config()

    # ── Option B: Custom callable ──
    if config.llm_function:
        try:
            result = config.llm_function(messages, **kwargs)
            if not isinstance(result, str):
                result = str(result)
            return result
        except Exception as e:
            logger.error('[LLMAdapter] Custom function failed: %s', e, exc_info=True)
            raise

    # ── Option A: OpenAI-compatible API ──
    if not config.llm_api_key:
        raise RuntimeError(
            'No LLM configured. Call configure(llm_api_key=...) or '
            'configure(llm_function=...) first.'
        )

    return _call_openai_compatible(
        messages,
        api_key=config.llm_api_key,
        base_url=config.llm_base_url,
        model=config.llm_model,
        temperature=kwargs.pop('temperature', config.llm_temperature),
        **kwargs,
    )


def _call_openai_compatible(messages, *, api_key, base_url, model,
                             temperature=0, **kwargs) -> str:
    """Call an OpenAI-compatible chat completions endpoint.

    Args:
        messages: Message list.
        api_key: API key for authentication.
        base_url: Base URL (e.g. 'https://api.openai.com/v1').
        model: Model name.
        temperature: Sampling temperature.
        **kwargs: Extra body parameters (stop, max_tokens, etc.).

    Returns:
        Assistant message content string.
    """
    url = f'{base_url.rstrip("/")}/chat/completions'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    body = {
        'model': model,
        'messages': messages,
        'temperature': temperature,
    }

    # Pass through supported extra params
    for key in ('stop', 'max_tokens', 'top_p', 'frequency_penalty',
                'presence_penalty'):
        if key in kwargs and kwargs[key] is not None:
            body[key] = kwargs[key]

    timeout = kwargs.get('timeout', 120)

    logger.debug('[LLMAdapter] POST %s model=%s msgs=%d', url[:80], model, len(messages))

    try:
        resp = requests.post(url, headers=headers, json=body,
                             timeout=(10, timeout))
        resp.raise_for_status()
        data = resp.json()
        content = data['choices'][0]['message']['content']
        usage = data.get('usage', {})
        logger.debug('[LLMAdapter] OK model=%s in_tok=%s out_tok=%s',
                     model,
                     usage.get('prompt_tokens', '?'),
                     usage.get('completion_tokens', '?'))
        return content or ''
    except requests.Timeout:
        logger.warning('[LLMAdapter] Timeout after %ds — model=%s', timeout, model)
        raise
    except requests.HTTPError as e:
        logger.error('[LLMAdapter] HTTP %d from %s: %s',
                     e.response.status_code if e.response else 0,
                     url[:80], str(e)[:300])
        raise
    except Exception as e:
        logger.error('[LLMAdapter] Request failed: %s', e, exc_info=True)
        raise
