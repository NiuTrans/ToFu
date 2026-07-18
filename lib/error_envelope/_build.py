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

logger = get_logger(__name__)


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
