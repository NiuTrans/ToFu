"""lib/embeddings.py — Embedding model client for text-embedding-{3-large,3-small,v4}.

Provides a simple async-friendly embedding client that:
  • Uses the dispatch system to route requests to the correct provider
  • Round-robins across API keys via dispatch slots
  • Batches texts for efficient embedding
  • Returns normalized numpy-free float vectors
  • Can be used for semantic search on conversations, skills, etc.

Usage:
    from lib.embeddings import embed_texts, cosine_similarity

    vecs = embed_texts(["hello world", "goodbye"])  # list of float lists
    sim = cosine_similarity(vecs[0], vecs[1])        # float in [-1, 1]
"""

import math
from typing import Optional

import requests

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'embed_texts',
    'embed_text',
    'cosine_similarity',
    'AVAILABLE_EMBEDDING_MODELS',
]

# Proxy bypass via centralized lib/proxy — respects Settings UI config.
from lib.proxy import proxies_for as _proxies_for

# text-embedding-v4 API limit: input length must be in [1, 8192] tokens.
# For mixed-language text (Chinese ≈ 1-2 tokens/char, English ≈ 4 chars/token),
# we use a conservative char limit to stay within 8192 tokens.
_MAX_INPUT_CHARS = 16_000   # ~8K tokens for English, safe for Chinese too

# Model info from benchmark (2025-03-21, re-tested):
# NOTE: Latency and RPM figures may drift over time as providers update infrastructure.
#       Re-benchmark periodically (e.g. quarterly) to keep these numbers accurate.
#   text-embedding-v4:      dim=1024, ~320ms,  ~105 RPM/key  ← fastest, recommended
#   text-embedding-3-small: dim=1536, ~3000ms, ~68 RPM/key   ← usable but slow
#   text-embedding-3-large: dim=3072, ~5000ms, ~32 RPM/key   ← usable, highest quality, slow
AVAILABLE_EMBEDDING_MODELS = {
    'text-embedding-v4':      {'dim': 1024, 'latency_ms': 320,  'max_rpm': 105, 'status': 'active'},
    'text-embedding-3-small': {'dim': 1536, 'latency_ms': 3000, 'max_rpm': 68,  'status': 'active'},
    'text-embedding-3-large': {'dim': 3072, 'latency_ms': 5000, 'max_rpm': 32,  'status': 'active'},
}

# Default model: text-embedding-v4 (fastest and recommended — good quality)
DEFAULT_MODEL = 'text-embedding-v4'


def _pick_embedding_slot(model: str):
    """Pick the best (api_key, base_url, key_name, slot) for an embedding request.

    Uses the dispatch system to find a slot with 'embedding' capability and
    the correct model.  Falls back to global LLM_BASE_URL + first API key
    if the dispatch system is unavailable.

    Returns:
        (api_key: str, base_url: str, key_name: str, slot_or_None)
    """
    try:
        from lib.llm_dispatch.factory import get_dispatcher
        dispatcher = get_dispatcher()
        slot = dispatcher.pick_and_reserve(
            capability='embedding',
            prefer_model=model,
        )
        if slot:
            base = slot.base_url.rstrip('/') if slot.base_url else ''
            if not base:
                from lib import LLM_BASE_URL
                base = LLM_BASE_URL.rstrip('/')
                logger.warning('[Embed] Slot %s:%s has no base_url, falling back to '
                               'LLM_BASE_URL=%s — may cause key/endpoint mismatch!',
                               slot.key_name, slot.model, base)
            return slot.api_key, base, slot.key_name, slot
    except Exception as e:
        logger.debug('[Embed] Dispatch unavailable, using fallback: %s', e)

    # Fallback: use global config
    from lib import LLM_API_KEYS, LLM_BASE_URL
    api_key = LLM_API_KEYS[0] if LLM_API_KEYS else ''
    base = LLM_BASE_URL.rstrip('/')
    return api_key, base, 'key_0', None


def embed_texts(
    texts: list[str],
    model: str = DEFAULT_MODEL,
    batch_size: int = 10,
    timeout: int = 30,
) -> list[list[float]]:
    """Embed a list of texts into vectors.

    Args:
        texts: List of strings to embed.
        model: Embedding model name.
        batch_size: Max texts per API call (API limit is 10 for this provider).
        timeout: Request timeout in seconds.

    Returns:
        List of float vectors, one per input text. Ordered same as input.
    """
    if not texts:
        return []

    # ── Validate & truncate inputs to stay within API token limit ──
    # The embedding API requires input length in [1, 8192] tokens.
    # Truncate overly long texts and replace empty strings with a space.
    sanitized = []
    for i, t in enumerate(texts):
        if not t or not t.strip():
            logger.debug('[Embed] Empty text at index %d, replacing with placeholder', i)
            sanitized.append(' ')  # API requires length >= 1
        elif len(t) > _MAX_INPUT_CHARS:
            logger.warning('[Embed] Text at index %d too long (%d chars), truncating to %d',
                           i, len(t), _MAX_INPUT_CHARS)
            sanitized.append(t[:_MAX_INPUT_CHARS])
        else:
            sanitized.append(t)
    texts = sanitized

    all_embeddings: list[list[float] | None] = [None] * len(texts)

    for batch_start in range(0, len(texts), batch_size):
        batch = texts[batch_start:batch_start + batch_size]

        # Pick the best slot for this batch via dispatch
        api_key, base_url, key_name, slot = _pick_embedding_slot(model)
        embed_url = f'{base_url}/embeddings'

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        }
        if slot and slot.extra_headers:
            headers.update(slot.extra_headers)
        body = {
            'model': model,
            'input': batch,
        }

        try:
            resp = requests.post(
                embed_url, headers=headers, json=body,
                proxies=_proxies_for(embed_url), timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get('data', []):
                    idx = item['index']
                    all_embeddings[batch_start + idx] = item['embedding']
                if slot:
                    slot.record_success(latency_ms=0)
                logger.debug('[Embed] Embedded batch of %d texts (%s, model=%s, url=%s)',
                             len(batch), key_name, model, embed_url)
            elif resp.status_code == 429:
                if slot:
                    slot.record_error(is_rate_limit=True)
                logger.warning('[Embed] 429 rate-limited on %s model=%s, retrying with next slot', key_name, model)
                # Retry with next slot
                api_key2, base_url2, key_name2, slot2 = _pick_embedding_slot(model)
                embed_url2 = f'{base_url2}/embeddings'
                headers2 = {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key2}',
                }
                if slot2 and slot2.extra_headers:
                    headers2.update(slot2.extra_headers)
                try:
                    resp2 = requests.post(
                        embed_url2, headers=headers2, json=body,
                        proxies=_proxies_for(embed_url2), timeout=timeout,
                    )
                    if resp2.status_code == 200:
                        data = resp2.json()
                        for item in data.get('data', []):
                            idx = item['index']
                            all_embeddings[batch_start + idx] = item['embedding']
                        if slot2:
                            slot2.record_success(latency_ms=0)
                        logger.debug('[Embed] Retry succeeded for batch (%s, model=%s, url=%s)',
                                     key_name2, model, embed_url2)
                    else:
                        if slot2:
                            slot2.record_error(is_rate_limit=False)
                        logger.error('[Embed] Retry also failed: HTTP %d (%s, url=%s) — batch indices %d-%d will use zero vectors',
                                     resp2.status_code, key_name2, embed_url2, batch_start, batch_start + len(batch) - 1)
                except Exception as retry_exc:
                    if slot2:
                        slot2.record_error(is_rate_limit=False)
                    logger.error('[Embed] Retry request failed (%s, url=%s): %s — batch indices %d-%d will use zero vectors',
                                 key_name2, embed_url2, retry_exc, batch_start, batch_start + len(batch) - 1, exc_info=True)
            else:
                if slot:
                    slot.record_error(is_rate_limit=False)
                logger.error('[Embed] HTTP %d from %s model=%s url=%s: %s',
                             resp.status_code, key_name, model, embed_url, resp.text[:200])
        except Exception as e:
            if slot:
                slot.record_error(is_rate_limit=False)
            logger.error('[Embed] Error embedding batch (%s, model=%s, url=%s): %s',
                         key_name, model, embed_url, e, exc_info=True)
            # Retry once with a different slot (the failed slot is now penalized
            # via record_error, so pick_and_reserve should choose a different one)
            try:
                api_key2, base_url2, key_name2, slot2 = _pick_embedding_slot(model)
                embed_url2 = f'{base_url2}/embeddings'
                headers2 = {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key2}',
                }
                if slot2 and slot2.extra_headers:
                    headers2.update(slot2.extra_headers)
                resp2 = requests.post(
                    embed_url2, headers=headers2, json=body,
                    proxies=_proxies_for(embed_url2), timeout=timeout,
                )
                if resp2.status_code == 200:
                    data2 = resp2.json()
                    for item in data2.get('data', []):
                        idx = item['index']
                        all_embeddings[batch_start + idx] = item['embedding']
                    if slot2:
                        slot2.record_success(latency_ms=0)
                    logger.info('[Embed] Retry succeeded for batch (%s, model=%s, url=%s)',
                                key_name2, model, embed_url2)
                else:
                    if slot2:
                        slot2.record_error(is_rate_limit=False)
                    logger.error('[Embed] Retry also failed: HTTP %d (%s, url=%s)',
                                 resp2.status_code, key_name2, embed_url2)
            except Exception as retry_exc:
                logger.error('[Embed] Retry request also failed: %s', retry_exc)

    # Fill any missing with zero vectors
    model_dim = AVAILABLE_EMBEDDING_MODELS.get(model, {}).get('dim', 1024)
    for i, emb in enumerate(all_embeddings):
        if emb is None:
            logger.warning('[Embed] Missing embedding for index %d, using zero vector', i)
            all_embeddings[i] = [0.0] * model_dim

    return all_embeddings


def embed_text(text: str, model: str = DEFAULT_MODEL, timeout: int = 30) -> list[float]:
    """Embed a single text. Convenience wrapper around embed_texts."""
    results = embed_texts([text], model=model, timeout=timeout)
    return results[0]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors. Returns float in [-1, 1]."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

