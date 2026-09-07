"""Per-model backend resolution.

Given a model id, return an **ordered list** of ``TokenCounter``
instances to try. The first one that returns a non-None count wins.

The order matters:
  1. Usage-cache first — ~exact, zero cost. Only works when caller
     supplied conv_id and we have fresh data.
  2. Family-specific exact tokenizer (deepseek/hf) if available.
  3. Upstream API (Claude, Gemini-native) — exact but network.
     Cheap estimate pre-filter avoids the round-trip when we're
     nowhere near the limit.
  4. tiktoken — universal, good-enough local fallback.
  5. Heuristic — final fallback, always succeeds.

Adding a new provider: drop a new ``XxxCounter`` in a new file, add
it to ``_ALL_COUNTERS`` below, and update ``resolve()`` with any
priority hints.
"""

from __future__ import annotations

import threading

from lib.log import get_logger

from .anthropic_api import AnthropicAPICounter
from .base import TokenCounter
from .deepseek_counter import DeepSeekCounter
from .gemini_api import GeminiAPICounter
from .heuristic import HeuristicCounter
from .hf_counter import HuggingFaceCounter
from .tiktoken_counter import TiktokenCounter
from .usage_cache import UsageCacheCounter

logger = get_logger(__name__)


_lock = threading.Lock()
_singletons: dict[str, TokenCounter] = {}


def _get(name: str, cls):
    """Return a cached singleton instance of ``cls`` keyed by ``name``."""
    with _lock:
        inst = _singletons.get(name)
        if inst is None:
            inst = cls()
            _singletons[name] = inst
        return inst


def _usage_cache() -> UsageCacheCounter:     return _get('usage_cache', UsageCacheCounter)
def _anthropic() -> AnthropicAPICounter:     return _get('anthropic_api', AnthropicAPICounter)
def _gemini() -> GeminiAPICounter:           return _get('gemini_api', GeminiAPICounter)
def _deepseek() -> DeepSeekCounter:          return _get('deepseek', DeepSeekCounter)
def _hf() -> HuggingFaceCounter:             return _get('hf', HuggingFaceCounter)
def _tiktoken() -> TiktokenCounter:          return _get('tiktoken', TiktokenCounter)
def _heuristic() -> HeuristicCounter:        return _get('heuristic', HeuristicCounter)


def _model_family(model: str) -> str:
    m = (model or '').lower()
    if 'claude' in m or 'anthropic' in m:   return 'claude'
    if 'gemini' in m:                        return 'gemini'
    if 'deepseek' in m:                      return 'deepseek'
    if 'qwen' in m:                          return 'qwen'
    if 'glm' in m:                           return 'glm'
    if 'doubao' in m:                        return 'doubao'
    if 'minimax' in m:                       return 'minimax'
    if 'llama' in m:                         return 'llama'
    if 'mistral' in m:                       return 'mistral'
    if 'gpt' in m or m.startswith('o1') or m.startswith('o3') or m.startswith('o4'):
        return 'openai'
    return 'generic'


def resolve(model: str) -> list[TokenCounter]:
    """Return the ordered counter list for ``model``.

    The caller (``api.count_tokens``) iterates this list and returns
    the first counter that produces a result.
    """
    family = _model_family(model)

    # Every model benefits from a usage-cache check first.
    ordered: list[TokenCounter] = [_usage_cache()]

    if family == 'claude':
        # Exact upstream API, then HF Claude mirror, then tiktoken, then heuristic.
        ordered += [_anthropic(), _hf(), _tiktoken(), _heuristic()]
    elif family == 'gemini':
        # Native countTokens only when connected directly — otherwise tiktoken.
        ordered += [_gemini(), _hf(), _tiktoken(), _heuristic()]
    elif family == 'deepseek':
        # Offline deepseek_tokenizer if installed, HF mirror, else tiktoken.
        ordered += [_deepseek(), _hf(), _tiktoken(), _heuristic()]
    elif family in ('qwen', 'glm', 'llama', 'mistral', 'doubao', 'minimax'):
        # HF tokenizer is exact for these when the tokenizer file is cached.
        ordered += [_hf(), _tiktoken(), _heuristic()]
    elif family == 'openai':
        # tiktoken is exact for OpenAI; HF is pointless here.
        ordered += [_tiktoken(), _heuristic()]
    else:
        # Unknown model — try tiktoken, then HF (in case we added mapping),
        # final fallback heuristic.
        ordered += [_tiktoken(), _hf(), _heuristic()]

    return ordered


def force_backend(name: str) -> list[TokenCounter]:
    """Return a single-element list for forced-mode overrides."""
    name = (name or '').lower()
    mapping = {
        'usage_cache': _usage_cache,
        'api':         _anthropic,    # legacy alias for Anthropic-only
        'anthropic':   _anthropic,
        'anthropic_api': _anthropic,
        'gemini':      _gemini,
        'gemini_api':  _gemini,
        'deepseek':    _deepseek,
        'hf':          _hf,
        'huggingface': _hf,
        'tiktoken':    _tiktoken,
        'heuristic':   _heuristic,
    }
    factory = mapping.get(name)
    if not factory:
        logger.warning('[TokenCounter] Unknown forced backend %r, using auto', name)
        return resolve('')
    return [factory()]


__all__ = ['resolve', 'force_backend']
