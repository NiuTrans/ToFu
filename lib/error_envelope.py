"""Typed error envelope for backend → frontend error reporting.

Every error surfaced to the user (in `task['error']`, in the `error`
field of SSE `done` / `state` events, in `/api/chat/poll` responses, and
in persisted `task_results.error` / `assistantMsg.error`) is a dict with
this shape::

    {
      'kind':      <enum>,           # machine-classifiable error category
      'severity':  'warning'|'error',# UI severity (color, icon)
      'retryable': bool,             # is "try again" likely to help?
      'message':   str,              # short bilingual title (one line)
      'hint':      str,              # bilingual recovery hint (multi-line)
      'detail':    str,              # technical detail (truncated raw text)
      'model':     str,              # model under which the error fired
      'context':   str,              # short tag, e.g. 'fallback', 'task-fatal'
      'source':    str,              # component that minted it
      'raw':       str,              # raw exception text (≤300 chars)
    }

The `kind` enum is closed — callers must pick one of these values:

  - ``quota``               persistent billing / balance exhaustion
  - ``ratelimit``           transient 429 / TPM-RPM throttle
  - ``permission``          401 / 403, key invalid or revoked
  - ``no_slot``             dispatch layer found zero usable slots
  - ``dispatch_exhausted``  every slot for this capability has been tried
  - ``timeout``             upstream / network read timeout
  - ``network``             connection error, DNS, proxy reset
  - ``content_filter``      provider safety filter (HTTP 450, etc.)
  - ``invalid_image``       image content rejected
  - ``prompt_too_long``     context window overflow (after auto-compact)
  - ``stream_only``         model rejects non-streaming
  - ``model_limit``         max_tokens exceeds learned model cap
  - ``tool_rounds_exhausted`` orchestrator hit the per-task tool budget
  - ``tool_timeout``        repeated tool-execution timeouts
  - ``premature_close``     SSE stream cut off (retries exhausted)
  - ``abnormal_stop``       missing finish marker, partial reply
  - ``aborted``             user clicked Stop (rare in error path)
  - ``server_offline``      frontend lost contact with the server
  - ``internal``            backend bug / unhandled exception
  - ``generic``             unrecognized — last-resort fallback

Backwards-compat note (2026-05-22): there is none.  The string form of
``task['error']`` was retired in favour of this dict.  Persistence
serializes the dict as JSON; the SSE / poll payloads carry the dict
verbatim; the frontend reducer reads ``error.message`` for display and
``error.kind`` / ``error.severity`` for classification.
"""

from __future__ import annotations

import json
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# Closed enum of valid `kind` values.  Used to validate envelopes at
# construction time so a typo (e.g. 'rateLimit') doesn't silently leak
# through to the UI as a generic error.
KINDS = frozenset({
    'quota', 'ratelimit', 'permission', 'no_slot', 'dispatch_exhausted',
    'timeout', 'network', 'content_filter', 'invalid_image',
    'prompt_too_long', 'stream_only', 'model_limit',
    'tool_rounds_exhausted', 'tool_timeout',
    'premature_close', 'abnormal_stop', 'aborted', 'server_offline',
    'internal', 'generic',
})

# Default severities — warnings are recoverable / user-actionable, errors
# usually mean the task ended in a non-recoverable state.
_WARNING_KINDS = frozenset({
    'ratelimit', 'no_slot', 'timeout', 'network',
    'tool_rounds_exhausted', 'tool_timeout',
    'premature_close', 'abnormal_stop',
    'aborted', 'server_offline',
})

# Kinds where retrying THE SAME REQUEST is genuinely likely to help
# (transient).  The frontend uses this to gate a "Retry" button.
_RETRYABLE_KINDS = frozenset({
    'ratelimit', 'no_slot', 'timeout', 'network',
    'premature_close', 'abnormal_stop', 'server_offline',
    'tool_timeout',
})


_SETTINGS_HINT_CN = (
    '• 打开 「设置 → Keys / Providers」，检查是否有 Key 被自动停用（429/余额耗尽），'
    '手动重新启用或添加新 Key。\n'
    '• 或者稍等几分钟，让 API 限额窗口重置后再试。\n'
    '• 若问题持续，可在设置中切换到其他可用模型 / Provider。'
)

_SETTINGS_HINT_EN = (
    '• Open "Settings → Keys / Providers" — check if any key was auto-disabled '
    '(429 / quota exhausted) and re-enable or add a new key.\n'
    '• Or wait a few minutes for the API rate-limit window to reset, then retry.\n'
    '• If the issue persists, switch to another available model / provider in Settings.'
)

_PERMISSION_HINT_CN = (
    '• 打开 「设置 → Keys」 检查该 Provider 的 Key 是否填写正确、是否被停用。\n'
    '• 若该 Key 对当前模型没有访问权限，请更换为其它模型或申请开通。'
)

_PERMISSION_HINT_EN = (
    '• Open "Settings → Keys" and verify the key for this provider is correct and enabled.\n'
    '• If the key does not have access to this model, switch models or request access.'
)

_TIMEOUT_HINT_CN = '• 稍后重试。若持续超时，可在 「设置 → 模型默认」 切换到响应更快的模型。'
_TIMEOUT_HINT_EN = ('• Retry shortly. If timeouts persist, switch to a faster model '
                    'in "Settings → Model defaults".')

_NETWORK_HINT_CN = '• 检查本机网络 / 代理设置，然后重试。'
_NETWORK_HINT_EN = '• Check your network / proxy settings, then retry.'


# ── Title / hint table indexed by kind ────────────────────────────────
# Each entry is (cn_title, en_title, cn_hint, en_hint).  Hints can be
# multi-line; titles are one line.

_TITLES: dict[str, tuple[str, str, str, str]] = {
    'quota':              ('⚠️ API Key 余额/配额已用尽',
                            'API key quota exhausted',
                            _SETTINGS_HINT_CN, _SETTINGS_HINT_EN),
    'ratelimit':          ('⚠️ API 请求已达限频（429）',
                            'API rate-limited (HTTP 429)',
                            _SETTINGS_HINT_CN, _SETTINGS_HINT_EN),
    'permission':         ('⚠️ API Key 被拒绝（401/403，无权限或已失效）',
                            'API key rejected (401/403, invalid or lacking permission)',
                            _PERMISSION_HINT_CN, _PERMISSION_HINT_EN),
    'no_slot':            ('⚠️ 当前没有可用的 API Key',
                            'No available API key slot',
                            _SETTINGS_HINT_CN, _SETTINGS_HINT_EN),
    'dispatch_exhausted': ('⚠️ 该模型所有 Key 的重试次数都已用尽',
                            'All keys for this model have been exhausted',
                            _SETTINGS_HINT_CN, _SETTINGS_HINT_EN),
    'timeout':            ('⚠️ 请求超时',
                            'Request timed out',
                            _TIMEOUT_HINT_CN, _TIMEOUT_HINT_EN),
    'network':            ('⚠️ 网络连接错误',
                            'Network connection error',
                            _NETWORK_HINT_CN, _NETWORK_HINT_EN),
    'content_filter':     ('⚠️ 该回复被模型安全过滤器拦截',
                            'Response blocked by the model\'s safety filter',
                            '• 尝试换一种方式提问，或切换到其他模型。',
                            '• Try rephrasing the question or switching to another model.'),
    'invalid_image':      ('⚠️ 图像内容被拒绝',
                            'Image rejected by the API',
                            '• 缩小或减少图片，再发送。',
                            '• Reduce image size or count and try again.'),
    'prompt_too_long':    ('⚠️ 上下文已超过模型上限',
                            'Context exceeds the model\'s limit',
                            '• 已尝试自动压缩仍失败，请清理对话历史或切换到上下文更大的模型。',
                            '• Auto-compaction did not free enough room — '
                            'trim the history or switch to a larger-context model.'),
    'stream_only':        ('⚠️ 该模型仅支持流式调用',
                            'Model only supports streaming',
                            '• 请切换到其他模型（系统已自动避开该模型）。',
                            '• Switch to another model (this one is auto-excluded).'),
    'model_limit':        ('⚠️ 输出长度超过模型上限',
                            'Output exceeds model max_tokens',
                            '• 系统已记住新的上限，可重试。',
                            '• The new limit has been recorded — retry.'),
    'tool_rounds_exhausted': ('⚠️ 工具调用轮数已达上限',
                               'Tool call round limit reached',
                               '• 模型未在限定轮数内得出最终答复，可点击 Continue 续写。',
                               '• The model did not finish within the per-task budget — '
                               'click Continue to extend.'),
    'tool_timeout':       ('⚠️ 工具调用连续超时',
                            'Repeated tool-execution timeouts',
                            '• 工具持续超时，建议简化任务或在 「设置 → 工具」 中调高超时时间。',
                            '• The tool keeps timing out — simplify the request or '
                            'raise the tool-timeout in Settings.'),
    'premature_close':    ('⚠️ 网关/代理过早关闭流',
                            'Gateway closed the stream prematurely',
                            '• 重试已用完，回复可能不完整。可点击 Retry 重新生成。',
                            '• Retries exhausted — the response may be incomplete. '
                            'Click Retry to regenerate.'),
    'abnormal_stop':      ('⚠️ API 流异常终止（缺失 finish 标记）',
                            'Stream ended without finish marker',
                            '• 回复可能不完整。可点击 Retry 重新生成。',
                            '• The reply may be truncated. Click Retry to regenerate.'),
    'aborted':            ('⏹️ 用户已中止',
                            'Stopped by user',
                            '', ''),
    'server_offline':     ('⚠️ 服务器离线',
                            'Server offline',
                            '• 等待服务器恢复后页面会自动重连，并尝试拉取已生成的内容。',
                            '• When the server comes back, this page will reconnect '
                            'automatically and try to recover any content that was generated.'),
    'internal':           ('⚠️ 内部错误',
                            'Internal error',
                            '• 请查看服务器日志（logs/error.log）了解详情。',
                            '• Check the server logs (logs/error.log) for details.'),
    'generic':            ('⚠️ 模型调用失败',
                            'LLM call failed',
                            _SETTINGS_HINT_CN, _SETTINGS_HINT_EN),
}


def _classify_exception(exc: BaseException) -> str:
    """Map an exception to a `kind` string.

    Recognizes both the typed exceptions in ``lib.llm_errors`` and the
    string-shaped errors that bubble up from the dispatch layer.
    """
    # First try the typed-exception path (preferred).
    try:
        from lib.llm import (
            AbortedError as _Abort,
            ContentFilterError as _CF,
            InvalidImageError as _Img,
            ModelLimitError as _Mlim,
            PermissionError_ as _Perm,
            PromptTooLongError as _Plong,
            RateLimitError as _RL,
            StreamOnlyError as _SO,
        )
    except Exception as _imp_err:
        logger.debug('lib.llm import failed in error classifier: %s', _imp_err)
        _Abort = _CF = _Img = _Mlim = _Perm = _Plong = _RL = _SO = None  # type: ignore

    if _Abort is not None and isinstance(exc, _Abort):
        return 'aborted'
    if _RL is not None and isinstance(exc, _RL):
        return 'quota' if getattr(exc, 'is_quota', False) else 'ratelimit'
    if _Perm is not None and isinstance(exc, _Perm):
        return 'permission'
    if _CF is not None and isinstance(exc, _CF):
        return 'content_filter'
    if _Img is not None and isinstance(exc, _Img):
        return 'invalid_image'
    if _Plong is not None and isinstance(exc, _Plong):
        return 'prompt_too_long'
    if _SO is not None and isinstance(exc, _SO):
        return 'stream_only'
    if _Mlim is not None and isinstance(exc, _Mlim):
        return 'model_limit'

    msg = str(exc).lower()
    tn = type(exc).__name__.lower()

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
    if (('insufficient' in msg and ('quota' in msg or 'balance' in msg))
            or 'credit_balance_too_low' in msg):
        return 'quota'
    if 'connectionerror' in tn or 'connection reset' in msg or 'connection aborted' in msg:
        return 'network'
    return 'generic'


def make_envelope(kind: str, *, message: str = '', detail: str = '',
                  model: str = '', context: str = '', source: str = '',
                  raw: str = '', severity: str | None = None,
                  retryable: bool | None = None,
                  hint: str | None = None) -> dict[str, Any]:
    """Build a typed error envelope.

    Most callers should prefer :func:`from_exception` — only use
    :func:`make_envelope` directly for non-exception failure paths
    (e.g. tool-rounds budget, tool execution timeout, content filter
    detected by absence-of-content rather than a thrown exception).

    Parameters
    ----------
    kind : str
        Closed enum — must be in :data:`KINDS`.  An unknown value is
        silently downgraded to ``'generic'`` and a warning is logged.
    message : str
        Override the default bilingual title.  Empty → use the default
        title for `kind`.
    detail : str
        Short technical detail line (e.g. ``'HTTP 429: rate_limit'``).
    model, context, source : str
        Diagnostic fields stored verbatim on the envelope.
    raw : str
        Raw exception text (truncated to 300 chars).
    severity : 'warning' | 'error' | None
        Override severity; default per-kind table.
    retryable : bool | None
        Override retryable flag; default per-kind table.
    hint : str | None
        Override the default bilingual hint.
    """
    if kind not in KINDS:
        logger.warning('[ErrorEnvelope] Unknown kind=%r — downgrading to generic', kind)
        kind = 'generic'

    cn_title, en_title, cn_hint, en_hint = _TITLES.get(
        kind, _TITLES['generic'])

    if not message:
        # Bilingual title joined with a newline so existing
        # white-space:pre-wrap renderers handle it without changes.
        if model:
            message = f'{cn_title}（模型：{model}）\n{en_title} (model: {model})'
        else:
            message = f'{cn_title}\n{en_title}'

    if hint is None:
        if cn_hint or en_hint:
            hint = (f'解决办法 / How to fix:\n{cn_hint}\n\n{en_hint}'
                    if cn_hint and en_hint else (cn_hint or en_hint))
        else:
            hint = ''

    if severity is None:
        severity = 'warning' if kind in _WARNING_KINDS else 'error'
    if retryable is None:
        retryable = kind in _RETRYABLE_KINDS

    raw = (raw or '')[:300]

    return {
        'kind':      kind,
        'severity':  severity,
        'retryable': bool(retryable),
        'message':   message,
        'hint':      hint,
        'detail':    (detail or '')[:300],
        'model':     model or '',
        'context':   context or '',
        'source':    source or '',
        'raw':       raw,
    }


def from_exception(exc: BaseException, *, model: str = '',
                   context: str = '', source: str = 'llm',
                   kind: str | None = None) -> dict[str, Any]:
    """Build an envelope from an exception, classifying its kind."""
    if kind is None:
        kind = _classify_exception(exc)
    raw = str(exc)
    return make_envelope(
        kind,
        detail=raw[:200],
        model=model,
        context=context,
        source=source,
        raw=raw,
    )


# ── Persistence helpers ────────────────────────────────────────────────
#
# `task_results.error` is `TEXT`.  Envelopes are stored as a JSON object
# string.  Older rows (pre-2026-05-22) may have a plain string — the
# loader normalises those into a `generic` envelope so the frontend's
# typed-reducer never has to handle two shapes.

def to_json(envelope: dict[str, Any] | str | None) -> str | None:
    """Serialise an envelope (or legacy string) for DB storage."""
    if envelope is None:
        return None
    if isinstance(envelope, str):
        # Legacy or short-circuit emit — wrap on the way out so the
        # database row is always a JSON object.
        envelope = make_envelope('generic', detail=envelope[:200], raw=envelope)
    try:
        return json.dumps(envelope, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.warning('[ErrorEnvelope] to_json failed: %s — falling back to string', e)
        return json.dumps(make_envelope(
            'internal',
            detail=f'Envelope serialise failed: {e}',
            raw=str(envelope)[:200],
        ), ensure_ascii=False)


def from_json(s: str | dict | None) -> dict[str, Any] | None:
    """Deserialise an envelope from DB storage.

    Accepts the two historical shapes:

      • JSON-serialised envelope dict (current format)
      • plain string (legacy ``task['error'] = 'something failed'``)

    Returns ``None`` for ``None`` / empty input.
    """
    if s is None or s == '':
        return None
    if isinstance(s, dict):
        return s
    if not isinstance(s, str):
        logger.debug('[ErrorEnvelope] from_json got unexpected type=%s', type(s).__name__)
        return make_envelope('generic', detail=str(s)[:200], raw=str(s))
    s = s.strip()
    if not s:
        return None
    if s[0] == '{':
        try:
            obj = json.loads(s)
            if isinstance(obj, dict) and 'kind' in obj:
                # Already an envelope — pass through.  Re-validate kind.
                if obj.get('kind') not in KINDS:
                    obj['kind'] = 'generic'
                return obj
            # JSON object that isn't a typed envelope (e.g. raw error
            # body from some other layer) — wrap.
            return make_envelope('generic', detail=s[:200], raw=s)
        except (json.JSONDecodeError, ValueError) as _e_audit:
            # Looked like JSON but wasn't — fall through to string wrap.
            logger.debug('[error_envelope] from_json caught %s: %s', type(_e_audit).__name__, _e_audit)
            pass
    # Plain legacy string — wrap as generic.
    return make_envelope('generic', detail=s[:200], raw=s)


def is_envelope(obj: Any) -> bool:
    """True iff ``obj`` looks like a typed error envelope."""
    return (isinstance(obj, dict)
            and isinstance(obj.get('kind'), str)
            and obj.get('kind') in KINDS)
