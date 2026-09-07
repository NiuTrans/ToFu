"""Tier 2 DeepSeek backend (offline, exact).

DeepSeek does NOT expose an online count_tokens endpoint; their docs
explicitly say "run the demo tokenizer code offline". The community
``deepseek_tokenizer`` pip package ships the BPE files and provides a
drop-in ``ds_token.encode(text)`` — identical to what the server uses.

Install: ``pip install deepseek_tokenizer`` (no runtime deps).
"""

from __future__ import annotations

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


_encoder = None
_probed = False


def _get_encoder():
    global _encoder, _probed
    if _probed:
        return _encoder
    _probed = True
    try:
        from deepseek_tokenizer import ds_token  # type: ignore
        _encoder = ds_token
        return ds_token
    except ImportError:
        logger.debug('[TokenCounter] deepseek_tokenizer not installed — '
                     'DeepSeek backend unavailable (will fall back to tiktoken)')
        return None
    except Exception as e:
        logger.warning('[TokenCounter] deepseek_tokenizer import failed: %s', e)
        return None


class DeepSeekCounter(TokenCounter):
    """Exact offline count for DeepSeek-family models."""

    name = 'deepseek_tokenizer'
    confidence = 'exact'
    needs_network = False

    def supports(self, model: str) -> bool:
        m = (model or '').lower()
        if not ('deepseek' in m or m.startswith('ds-')):
            return False
        return _get_encoder() is not None

    def count(self, messages: list, *, model: str,
              system: Any = None, tools: Any = None,
              **kwargs) -> Optional[int]:
        enc = _get_encoder()
        if enc is None:
            return None
        try:
            total = 0
            per_msg_overhead = 4
            for txt in iter_message_texts(messages, system, tools):
                total += len(enc.encode(txt))
            total += per_msg_overhead * (len(messages) if messages else 0)
            total += count_images(messages) * _IMAGE_TOKENS_DEFAULT
            total += _STRUCTURAL_OVERHEAD_TOKENS
            return total
        except Exception as e:
            logger.warning('[TokenCounter] deepseek_tokenizer count failed: %s', e)
            return None


__all__ = ['DeepSeekCounter']
