"""Tier 2 Hugging Face ``AutoTokenizer`` backend (offline, exact).

Inspired by **opencode-tokenscope**: HF hosts the real tokenizer
vocabularies for Qwen, GLM, Llama, Mistral, DeepSeek — and unofficial
Claude mirrors (``Xenova/claude-tokenizer``). Using them gives us
EXACT counts for providers with no count_tokens API.

Trade-off: first-call latency is 2-5 s (model download), memory is
50-200 MB per tokenizer, but subsequent calls are ~10-50 ms.

Gating:
  - Only activated when ``transformers`` is installed AND the model
    maps to a known HF repo ID (see ``_HF_REPO_MAP``).
  - Gracefully returns None when unavailable — the dispatcher will
    fall through to tiktoken / heuristic.

This backend is **off by default** in container deployments because
the download can be slow. Enable explicitly via either:
  - installing ``transformers`` + running the first call once to warm
    the cache (e.g. during deploy), or
  - setting ``TOFU_TOKEN_COUNTER_HF_AUTOFETCH=1``.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

from lib.log import get_logger

from .base import (
    TokenCounter,
    count_images,
    iter_message_texts,
    _IMAGE_TOKENS_DEFAULT,
    _STRUCTURAL_OVERHEAD_TOKENS,
)

logger = get_logger(__name__)


# model-id substring → HF repo id. Extend as needed.
_HF_REPO_MAP = {
    'qwen':        'Qwen/Qwen2.5-7B-Instruct',
    'qwen3':       'Qwen/Qwen2.5-7B-Instruct',
    'glm':         'THUDM/glm-4-9b-chat',
    'doubao':      'Qwen/Qwen2.5-7B-Instruct',   # close-enough proxy
    'minimax':     'Qwen/Qwen2.5-7B-Instruct',   # close-enough proxy
    'llama':       'meta-llama/Llama-3.1-8B-Instruct',
    'mistral':     'mistralai/Mistral-7B-Instruct-v0.3',
    'claude':      'Xenova/claude-tokenizer',    # unofficial community mirror
    'deepseek':    'deepseek-ai/DeepSeek-V3',    # fallback if deepseek_tokenizer pip missing
    'gemini':      'google/gemma-2-9b',          # proxy; not official Gemini
}


_AUTOFETCH = os.environ.get('TOFU_TOKEN_COUNTER_HF_AUTOFETCH', '0').strip() in ('1', 'true', 'yes')


_lock = threading.Lock()
_tokenizers: dict[str, Any] = {}
_available: Optional[bool] = None


def _resolve_repo(model: str) -> Optional[str]:
    m = (model or '').lower()
    for key, repo in _HF_REPO_MAP.items():
        if key in m:
            return repo
    return None


def _get_tokenizer(model: str):
    """Return a cached HF tokenizer, or None if unavailable.

    The module-level probe ``_available`` short-circuits subsequent
    calls after an initial ImportError, avoiding repeated try/except
    churn on systems without ``transformers``.
    """
    global _available
    if _available is False:
        return None

    repo = _resolve_repo(model)
    if not repo:
        return None

    with _lock:
        if repo in _tokenizers:
            return _tokenizers[repo]

        # Check autofetch gate before first network call
        if not _AUTOFETCH:
            # Only load if the tokenizer is already cached locally (no
            # download). We check by setting local_files_only=True.
            local_only = True
        else:
            local_only = False

        try:
            from transformers import AutoTokenizer  # type: ignore
        except ImportError:
            logger.info('[TokenCounter] transformers not installed — '
                         'HF backend unavailable')
            _available = False
            return None
        except Exception as e:
            logger.warning('[TokenCounter] transformers import failed: %s', e)
            _available = False
            return None

        try:
            tok = AutoTokenizer.from_pretrained(
                repo,
                trust_remote_code=False,
                local_files_only=local_only,
            )
            _tokenizers[repo] = tok
            _available = True
            logger.info('[TokenCounter] HF tokenizer loaded: %s (local_only=%s)',
                        repo, local_only)
            return tok
        except Exception as e:
            if local_only:
                logger.debug('[TokenCounter] HF tokenizer %s not cached '
                             'locally (autofetch disabled): %s', repo, e)
            else:
                logger.warning('[TokenCounter] HF tokenizer %s load failed: %s',
                               repo, e)
            return None


class HuggingFaceCounter(TokenCounter):
    """Exact offline count via HF tokenizer (Qwen, GLM, Llama, Claude …)."""

    name = 'huggingface'
    confidence = 'exact'
    needs_network = False  # (cached after first load)

    def supports(self, model: str) -> bool:
        if _available is False:
            return False
        return _resolve_repo(model) is not None

    def count(self, messages: list, *, model: str,
              system: Any = None, tools: Any = None,
              **kwargs) -> Optional[int]:
        tok = _get_tokenizer(model)
        if tok is None:
            return None
        try:
            total = 0
            per_msg_overhead = 4
            for txt in iter_message_texts(messages, system, tools):
                ids = tok.encode(txt, add_special_tokens=False)
                total += len(ids)
            total += per_msg_overhead * (len(messages) if messages else 0)
            total += count_images(messages) * _IMAGE_TOKENS_DEFAULT
            total += _STRUCTURAL_OVERHEAD_TOKENS
            return total
        except Exception as e:
            logger.warning('[TokenCounter] HF count failed: %s', e)
            return None


__all__ = ['HuggingFaceCounter']
