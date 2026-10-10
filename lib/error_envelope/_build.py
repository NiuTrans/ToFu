"""Envelope builders: :func:`make_envelope` (from raw fields) and
:func:`from_exception` (classify + build from an exception).
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

from lib.error_envelope._classify import _classify_exception
from lib.error_envelope._constants import (
    KINDS,
    _RETRYABLE_KINDS,
    _TITLES,
    _WARNING_KINDS,
)
from lib.error_envelope._schema import (
    _CORE_FIELDS,
    _is_complete_envelope,
)

logger = get_logger(__name__)


def make_envelope(kind: str, *, message: str = '', detail: str = '',
                  model: str = '', context: str = '', source: str = '',
                  raw: str = '', severity: str | None = None,
                  retryable: bool | None = None,
                  hint: str | None = None,
                  title_key: str | None = None,
                  hint_key: str | None = None,
                  extensions: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a typed error envelope.

    Most callers should prefer :func:`from_exception` — only use
    :func:`make_envelope` directly for non-exception failure paths
    (e.g. a legacy persisted tool-round status, tool execution timeout, content filter
    detected by absence-of-content rather than a thrown exception).

    Parameters
    ----------
    kind : str
        Closed enum — must be in :data:`KINDS`.  An unknown value is
        silently downgraded to ``'generic'`` and a warning is logged.
    message : str
        Override the default bilingual title.  Empty → use the default
        title for `kind`.  A custom message is rendered verbatim by every
        client, so no ``titleKey`` is emitted for it.
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
        Override the default bilingual hint.  A custom hint is rendered
        verbatim UNLESS an explicit `hint_key` is also passed.
    title_key, hint_key : str | None
        i18n keys the frontend resolves in the CURRENT UI language
        (default ``err.k.<kind>.title`` / ``err.k.<kind>.hint``).
        The legacy bilingual ``message`` / ``hint`` fields are ALWAYS
        populated byte-identically for headless clients and as the
        fallback when the frontend's i18n table predates the key.
    extensions : dict | None
        Optional domain/wire metadata (for example ``retry_after_s`` or a
        versioned orchestration ``outcome``). Core envelope fields cannot be
        overridden through this map.
    """
    if kind not in KINDS:
        logger.warning('[ErrorEnvelope] Unknown kind=%r — downgrading to generic', kind)
        kind = 'generic'

    # Remember whether the caller overrode the text BEFORE defaulting — a
    # custom message/hint renders verbatim and must not carry a key that
    # would make the frontend ignore the override (unless the caller
    # explicitly paired one with it).
    _message_overridden = bool(message)
    _hint_overridden = hint is not None

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

    envelope = {
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
    # Keyed i18n surface: the modern frontend renders title/hint in the
    # current UI language via these keys; `message`/`hint` stay bilingual
    # byte-identical for headless clients and old frontend bundles.
    if not _message_overridden:
        envelope['titleKey'] = title_key or f'err.k.{kind}.title'
    # A custom hint with no explicit key renders verbatim — emitting the
    # default key here would make the frontend IGNORE the override.
    if hint_key is None and not _hint_overridden:
        hint_key = f'err.k.{kind}.hint'
    if hint_key:
        envelope['hintKey'] = hint_key
    if extensions:
        envelope.update({
            key: value for key, value in extensions.items()
            if key not in _CORE_FIELDS
        })
    return envelope


def from_exception(exc: BaseException, *, model: str = '',
                   context: str = '', source: str = 'llm',
                   kind: str | None = None) -> dict[str, Any]:
    """Build an envelope from an exception, classifying its kind."""
    if kind is None:
        kind = _classify_exception(exc)
    raw = str(exc)
    status_code = getattr(exc, 'status_code', 0)

    delivery_types = {cls.__name__ for cls in type(exc).__mro__}
    if 'EventDeliveryError' in delivery_types or 'EventDeliveryRevoked' in delivery_types:
        # The coordinator's wrapper carries subsystem identity, while its
        # explicit cause retains the first failed write/worker-start detail.
        # Do not classify the cause: a storage timeout is not a model timeout.
        source = 'task-event-delivery'
        model = ''
        if exc.__cause__ is not None:
            raw = f'{raw}: {type(exc.__cause__).__name__}: {exc.__cause__}'
    extensions = None
    message, hint = '', None
    from lib.llm_errors import ProviderToolCallFloodError, RequestTransportError
    if isinstance(exc, ProviderToolCallFloodError):
        message = ('⚠️ 响应中重复工具调用超限，已主动停止\n'
                   'Repeated tool calls exceeded the response limit; stopped locally')
        hint = ('同一响应包含过多同参数调用，未采纳该工具批次。'
                '可手动重试；若再次发生，可切换模型或供应商。'
                '这不表示服务宕机，重复来源请查看请求溯源。\n'
                'The repeated tool batch was not adopted. Retry manually, or '
                'switch model/provider if it recurs. This does not indicate an '
                'outage; inspect request evidence for the repetition source.')
        extensions = {'failureStage': exc.failure_stage,
                      'providerErrorCode': 'tool_call_flood',
                      'identicalToolCalls': exc.count,
                      'identicalToolCallLimit': exc.limit,
                      'autoRetryExhausted': True}
        evidence = getattr(exc, 'tool_call_evidence', None)
        if isinstance(evidence, dict):
            extensions['toolCallEvidence'] = evidence
        archive_ref = getattr(exc, 'raw_archive_ref', None)
        if isinstance(archive_ref, dict):
            extensions['rawArchiveRef'] = archive_ref
    if isinstance(exc, RequestTransportError):
        extensions = {'failureStage': exc.failure_stage,
                      'transportReason': exc.transport_reason,
                      'transportCause': exc.cause_type}
        if exc.request_bytes is not None:
            extensions['requestBytes'] = exc.request_bytes
        if exc.timeout_seconds is not None:
            extensions['timeoutSeconds'] = exc.timeout_seconds
        if exc.failure_stage == 'upload':
            message = '⚠️ 请求正文上传超时\nRequest upload timed out'
            hint = ('检查到模型网关的上传链路；可缩减图片或上下文后重试。\n'
                    'Check upload connectivity to the model gateway; '
                    'reduce images or context before retrying.')
    if (isinstance(status_code, int) and not isinstance(status_code, bool)
            and 100 <= status_code <= 599):
        extensions = {'statusCode': status_code}
    from lib.llm_errors import (LocalRequestPreparationError,
                                SubscriptionCredentialUnavailableError)
    credential_error = exc.__cause__ if isinstance(exc, LocalRequestPreparationError) else exc
    severity, retryable = None, None
    if isinstance(credential_error, SubscriptionCredentialUnavailableError):
        message = ('⚠️ 无法读取 Codex 共享登录凭据\n'
                   'Codex shared sign-in credentials unavailable')
        hint = ('尚未向模型发送请求。可重试；若持续失败，请检查「设置 → 订阅登录」中的'
                '共享登录来源，并查看错误详情中的原因。无需仅凭此提示重新登录。\n'
                'The model request was not sent. Retry; if it persists, check the shared '
                'sign-in source in Settings → Subscription sign-in and the reason in '
                'error details. This failure alone does not establish session expiry.')
        severity, retryable = 'warning', credential_error.transient
        extensions = {'credentialSource': credential_error.credential_source,
                      'authIssue': credential_error.reason,
                      'failureStage': 'subscription_credentials',
                      'providerReached': False}
    return make_envelope(
        kind,
        severity=severity,
        retryable=retryable,
        message=message,
        hint=hint,
        detail=raw[:200],
        model=model,
        context=context,
        source=source,
        raw=raw,
        extensions=extensions,
    )


def normalize_envelope(error: Any, *, context: str = '',
                       source: str = '',
                       require_complete: bool = False) \
        -> dict[str, Any] | None:
    """Normalize any error value through one closed-kind boundary.

    Complete valid envelopes retain object identity. Rebuilt dictionaries
    keep additive domain fields such as orchestration's versioned ``outcome``
    while core envelope fields are regenerated and validated here.
    """
    if error is None:
        return None
    if isinstance(error, dict):
        kind = error.get('kind')
        message = error.get('message')
        if (kind in KINDS and isinstance(message, str) and message
                and (not require_complete or _is_complete_envelope(error))):
            return error
        normalized = make_envelope(
            kind if kind in KINDS else 'generic',
            message=message if isinstance(message, str) and message else '',
            detail=str(error.get('detail') or '')[:300],
            model=str(error.get('model') or ''),
            context=str(error.get('context') or context),
            source=str(error.get('source') or source),
            raw=str(error.get('raw') or error)[:300],
            severity=(error.get('severity')
                      if error.get('severity') in ('warning', 'error')
                      else None),
            retryable=(error.get('retryable')
                       if isinstance(error.get('retryable'), bool) else None),
            hint=(error.get('hint')
                  if isinstance(error.get('hint'), str) else None),
        )
        normalized.update({
            key: value for key, value in error.items()
            if key not in _CORE_FIELDS
        })
        return normalized
    if isinstance(error, BaseException):
        return from_exception(error, context=context, source=source)
    if isinstance(error, str):
        if error in KINDS:
            return make_envelope(error, context=context, source=source)
        return make_envelope(
            'generic', detail=error, context=context, source=source,
            raw=error)
    raw = str(error)
    return make_envelope(
        'generic', detail=raw[:300], context=context, source=source,
        raw=raw[:300])
