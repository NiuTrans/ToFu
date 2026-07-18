"""Exception → ``kind`` classification for the error envelope.

Recognizes both the typed exceptions in ``lib.llm`` and the string-shaped
errors that bubble up from the dispatch layer.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


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
            EndpointUnreachableError as _Unreach,
            InvalidImageError as _Img,
            ModelLimitError as _Mlim,
            PermissionError_ as _Perm,
            PromptTooLongError as _Plong,
            RateLimitError as _RL,
            StreamOnlyError as _SO,
        )
    except Exception as _imp_err:
        logger.debug('lib.llm import failed in error classifier: %s', _imp_err)
        _Abort = _CF = _Img = _Mlim = _Perm = _Plong = _RL = _SO = _Unreach = None  # type: ignore

    if _Abort is not None and isinstance(exc, _Abort):
        return 'aborted'
    # Endpoint-unreachable must be checked BEFORE the string-based
    # timeout/network heuristics below — its message contains both
    # "unreachable" and "timed out"/"connect" substrings that would
    # otherwise misclassify it as a transient read-timeout.
    if _Unreach is not None and isinstance(exc, _Unreach):
        return 'endpoint_unreachable'
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
    if 'endpointunreachable' in tn or 'endpoint unreachable' in msg or 'are unreachable' in msg:
        return 'endpoint_unreachable'
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

    # Python programming-error builtins are OUR OWN code bugs (e.g. a
    # ``TypeError: __new__() missing 1 required positional argument`` from a
    # str-subclass deepcopy, conv mrova3t92jffm7) — never a quota / rate-limit
    # / key problem. Route them to 'internal' so the user-facing hint says
    # "check the server logs" instead of the misleading generic
    # Settings→Keys / 429 quota advice. Note: ``RuntimeError`` / bare
    # ``Exception`` are deliberately EXCLUDED — the dispatch layer raises
    # those as string-shaped errors that the substring heuristics above are
    # meant to classify; only leaf builtin defects fall here.
    if isinstance(exc, (TypeError, AttributeError, KeyError, IndexError,
                        NameError, UnboundLocalError, ValueError,
                        AssertionError, ZeroDivisionError)):
        return 'internal'

    return 'generic'
