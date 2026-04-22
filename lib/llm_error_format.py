"""User-facing formatting for LLM dispatch / streaming errors.

When every API key of the selected model has been exhausted (rate-limited,
quota-depleted, permission-denied, etc.), the user sees a raw error like
``All 3 dispatch_stream attempts failed for capability=text`` which is
developer-speak and gives no recovery path.

This module provides :func:`format_llm_error_for_user` — it inspects the
exception (type + message) and returns a concise, bilingual, actionable
error block that:

- Names the most likely cause in plain language.
- Tells the user what to do next (go to Settings to enable more keys, or
  wait for rate-limit window to reset).
- Still includes the raw technical detail at the bottom so an operator
  grepping the frontend screenshot can still diagnose.

The formatter is deliberately type-duck: it works on any ``Exception`` and
falls back to a generic template when the signature is unrecognised.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


_SETTINGS_HINT = (
    '• 打开 「设置 → Keys / Providers」，检查是否有 Key 被自动停用（429/余额耗尽），'
    '手动重新启用或添加新 Key。\n'
    '• 或者稍等几分钟，让 API 限额窗口重置后再试。\n'
    '• 若问题持续，可在设置中切换到其他可用模型 / Provider。'
)

_SETTINGS_HINT_EN = (
    '• Open “Settings → Keys / Providers” — check if any key was auto-disabled '
    '(429 / quota exhausted) and re-enable or add a new key.\n'
    '• Or wait a few minutes for the API rate-limit window to reset, then retry.\n'
    '• If the issue persists, switch to another available model / provider in Settings.'
)


def _classify(exc: BaseException) -> str:
    """Return a short tag describing the error family.

    Tags: ``quota`` | ``ratelimit`` | ``permission`` | ``no_slot`` |
    ``dispatch_exhausted`` | ``timeout`` | ``network`` | ``generic``.
    """
    try:
        from lib.llm_client import (
            PermissionError_ as _Perm,
            RateLimitError as _RL,
        )
    except Exception as _imp_err:
        logger.debug('llm_client import failed in error classifier: %s', _imp_err)
        _Perm = _RL = None  # type: ignore

    if _RL is not None and isinstance(exc, _RL):
        return 'quota' if getattr(exc, 'is_quota', False) else 'ratelimit'
    if _Perm is not None and isinstance(exc, _Perm):
        return 'permission'

    msg = str(exc).lower()
    tn = type(exc).__name__.lower()

    # Dispatch layer gave up after retries — very common case when all keys 429'd
    if 'all ' in msg and 'dispatch' in msg and 'attempts failed' in msg:
        return 'dispatch_exhausted'
    if 'no slot' in msg or 'no_slot' in msg:
        return 'no_slot'
    if 'timed out' in msg or 'timeout' in tn or 'timeout' in msg:
        return 'timeout'
    if '429' in msg or 'rate limit' in msg or 'rate-limit' in msg or 'too many requests' in msg:
        return 'ratelimit'
    if '401' in msg or '403' in msg or 'unauthorized' in msg or 'forbidden' in msg:
        return 'permission'
    if ('insufficient' in msg and ('quota' in msg or 'balance' in msg)) or 'credit_balance_too_low' in msg:
        return 'quota'
    if 'connectionerror' in tn or 'connection reset' in msg or 'connection aborted' in msg:
        return 'network'
    return 'generic'


def format_llm_error_for_user(exc: BaseException, *, model: str = '',
                              context: str = '') -> str:
    """Format a user-facing error message for the frontend error-block.

    Parameters
    ----------
    exc : BaseException
        The exception raised by the LLM dispatch / streaming layer.
    model : str
        The model that was being called (shown to help users pick a
        different one in Settings).
    context : str
        Optional short context tag (e.g. ``'fallback'``, ``'post-loop'``)
        prepended to the title so operators can tell which code path fired.

    Returns
    -------
    str
        Multi-line bilingual message safe to drop into ``task['error']``.
        Uses ``\\n`` newlines — the frontend CSS renders them via
        ``white-space:pre-wrap`` on ``.error-block``.
    """
    tag = _classify(exc)
    raw = str(exc)[:300].strip()
    model_suffix = f'（模型：{model}）' if model else ''
    model_suffix_en = f' (model: {model})' if model else ''
    ctx_prefix = f'[{context}] ' if context else ''

    if tag in ('quota', 'ratelimit', 'dispatch_exhausted', 'no_slot'):
        if tag == 'quota':
            title_cn = '⚠️ API Key 余额/配额已用尽'
            title_en = 'API key quota exhausted'
        elif tag == 'ratelimit':
            title_cn = '⚠️ API 请求已达限频（429）'
            title_en = 'API rate-limited (HTTP 429)'
        elif tag == 'no_slot':
            title_cn = '⚠️ 当前没有可用的 API Key'
            title_en = 'No available API key slot'
        else:  # dispatch_exhausted
            title_cn = '⚠️ 该模型所有 Key 的重试次数都已用尽'
            title_en = 'All keys for this model have been exhausted'

        return (
            f'{ctx_prefix}{title_cn}{model_suffix}\n'
            f'{title_en}{model_suffix_en}\n\n'
            f'解决办法 / How to fix:\n'
            f'{_SETTINGS_HINT}\n\n'
            f'{_SETTINGS_HINT_EN}\n\n'
            f'技术细节 / Technical detail: {raw}'
        )

    if tag == 'permission':
        return (
            f'{ctx_prefix}⚠️ API Key 被拒绝（401/403，无权限或已失效）{model_suffix}\n'
            f'API key rejected (401/403, invalid or lacking permission){model_suffix_en}\n\n'
            f'解决办法 / How to fix:\n'
            f'• 打开 「设置 → Keys」 检查该 Provider 的 Key 是否填写正确、是否被停用。\n'
            f'• 若该 Key 对当前模型没有访问权限，请更换为其它模型或申请开通。\n\n'
            f'• Open “Settings → Keys” and verify the key for this provider is correct and enabled.\n'
            f'• If the key does not have access to this model, switch models or request access.\n\n'
            f'技术细节 / Technical detail: {raw}'
        )

    if tag == 'timeout':
        return (
            f'{ctx_prefix}⚠️ 请求超时{model_suffix}\n'
            f'Request timed out{model_suffix_en}\n\n'
            f'解决办法 / How to fix:\n'
            f'• 稍后重试。若持续超时，可在 「设置 → 模型默认」 切换到响应更快的模型。\n'
            f'• Retry shortly. If timeouts persist, switch to a faster model in '
            f'“Settings → Model defaults”.\n\n'
            f'技术细节 / Technical detail: {raw}'
        )

    if tag == 'network':
        return (
            f'{ctx_prefix}⚠️ 网络连接错误{model_suffix}\n'
            f'Network connection error{model_suffix_en}\n\n'
            f'解决办法 / How to fix:\n'
            f'• 检查本机网络 / 代理设置，然后重试。\n'
            f'• Check your network / proxy settings, then retry.\n\n'
            f'技术细节 / Technical detail: {raw}'
        )

    # Generic fallback — still give the user actionable hints.
    return (
        f'{ctx_prefix}⚠️ 模型调用失败{model_suffix}\n'
        f'LLM call failed{model_suffix_en}\n\n'
        f'可以尝试 / You can try:\n'
        f'{_SETTINGS_HINT}\n\n'
        f'{_SETTINGS_HINT_EN}\n\n'
        f'技术细节 / Technical detail: {raw}'
    )
