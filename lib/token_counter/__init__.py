"""Authoritative token counting — modular backend package.

Centralised entry point for "how many tokens will this request cost?".
Replaces the naïve ``chars/4`` estimator that under-counted CJK-heavy
conversations by 2.5× and caused the ``conv=mo4fr5xeup9ogp`` compaction
failure (see 2026-05-04 post-mortem).

Per-provider support matrix (see
``<project>/.tofu/memories/token-counter-api-support-matrix.md`` for full
details and verification dates):

  ==============  ==============================  ==================
  Provider        Best available counter          Confidence
  ==============  ==============================  ==================
  Anthropic       Upstream ``count_tokens`` API   exact
                  (proxied by Meituan gateway)
  AWS Bedrock     (same answer via Anthropic)     exact
  Google Gemini   Native ``countTokens`` — ONLY   exact
                  when connected directly to
                  Google; Meituan gateway skips
                  it (gracefully → tiktoken).
  OpenAI (GPT-*)  ``tiktoken`` (official)         exact
  DeepSeek        ``deepseek_tokenizer`` pip      exact
                  (official offline package)
  Qwen / GLM /    HF ``AutoTokenizer`` when       exact (if HF
  Llama / Mistral  tokenizer cached; else         cached) / good
  / Doubao /       tiktoken cl100k (~±10%)
  MiniMax
  Unknown         tiktoken → heuristic            good → approx
  ==============  ==============================  ==================

Modular design — each backend is one file. Priority per model is
decided in ``resolver.py``; adding a backend is one new file +
one line in the resolver.

Environment variables:

  TOFU_TOKEN_COUNTER
      Forced mode. ``auto`` (default), ``api``, ``anthropic_api``,
      ``gemini_api``, ``tiktoken``, ``deepseek``, ``hf``,
      ``usage_cache``, ``heuristic``.

  TOFU_TOKEN_COUNTER_API_TIMEOUT
      Timeout (sec) for the network tiers. Default 10.

  TOFU_TOKEN_COUNTER_API_THRESHOLD
      Skip network tiers when cheap estimate is below this fraction
      of the model's context limit. Default 0.50.

  TOFU_TOKEN_COUNTER_CACHE_TTL
      Seconds a recorded ``usage`` stays authoritative. Default 3600.

  TOFU_TOKEN_COUNTER_HF_AUTOFETCH
      When ``1``, allow HF ``AutoTokenizer.from_pretrained()`` to
      download the tokenizer on first use (otherwise requires a
      locally-cached copy). Default off.

Public API (imported from ``lib.token_counter``):

  :func:`count_tokens`   Full-request counter (messages + system + tools).
  :func:`count_text`     Fast single-string count.
  :func:`record_usage`   Feed the last response's usage into the cache.
                         Called from ``lib/llm/stream.py`` after every
                         successful stream.
  :func:`invalidate`     Drop the usage cache for a conv (after compact).
"""

from __future__ import annotations

from .api import count_tokens, count_text, invalidate, record_usage
from .base import CountResult

__all__ = [
    'count_tokens', 'count_text',
    'record_usage', 'invalidate',
    'CountResult',
]
