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
            BadRequestError as _BR,
            ContentFilterError as _CF,
            ContextCompactionError as _Compact,
            EndpointUnreachableError as _Unreach,
            InvalidImageError as _Img,
            ModelLimitError as _Mlim,
            PermissionError_ as _Perm,
            PromptTooLongError as _Plong,
            RateLimitError as _RL,
            RequestScopedError as _Req,
            RetryableAPIError as _Retry,
            StreamOnlyError as _SO,
        )
    except Exception as _imp_err:
        logger.debug('lib.llm import failed in error classifier: %s', _imp_err)
        _Abort = _BR = _CF = _Compact = _Img = _Mlim = _Perm = _Plong = _RL = _Req = _Retry = _SO = _Unreach = None  # type: ignore

    if _Abort is not None and isinstance(exc, _Abort):
        return 'aborted'
    # Endpoint-unreachable must be checked BEFORE the string-based
    # timeout/network heuristics below — its message contains both
    # "unreachable" and "timed out"/"connect" substrings that would
    # otherwise misclassify it as a transient read-timeout.
    if _Unreach is not None and isinstance(exc, _Unreach):
        return 'endpoint_unreachable'
    if _RL is not None and isinstance(exc, _RL):
        # A gateway-shaped RateLimitError (vendor 401/403/429 surfaced through
        # the upstream-vendor-transient raise path, is_gateway=True) is a
        # VENDOR OUTAGE, not a per-key 429 — mapping it to 'ratelimit' sends
        # the user to Settings → Keys for a problem their keys cannot fix.
        if getattr(exc, 'is_gateway', False):
            return 'upstream_error'
        return 'quota' if getattr(exc, 'is_quota', False) else 'ratelimit'
    # 5xx-after-retries: same vendor-outage truth as the gateway RL above.
    if _Retry is not None and isinstance(exc, _Retry):
        return 'upstream_error'
    if (_BR is not None and isinstance(exc, _BR)) or \
            (_Req is not None and isinstance(exc, _Req)):
        return 'bad_request'
    if _Perm is not None and isinstance(exc, _Perm):
        return 'permission'
    if _CF is not None and isinstance(exc, _CF):
        return 'content_filter'
    if _Img is not None and isinstance(exc, _Img):
        return 'invalid_image'
    if _Compact is not None and isinstance(exc, _Compact):
        return 'internal'
    if _Plong is not None and isinstance(exc, _Plong):
        return 'prompt_too_long'
    if _SO is not None and isinstance(exc, _SO):
        return 'stream_only'
    if _Mlim is not None and isinstance(exc, _Mlim):
        return 'model_limit'

    # Python programming-error builtins are OUR OWN code bugs (e.g. a
    # ``TypeError: __new__() missing 1 required positional argument`` from a
    # str-subclass deepcopy, conv mrova3t92jffm7). This type identity outranks
    # incidental words in the exception message: ``ValueError('HTTP 429')`` is
    # still a programming defect, not a provider throttle. ``RuntimeError`` /
    # bare ``Exception`` remain deliberately excluded because the dispatch
    # layer uses those for legacy string-shaped upstream failures.
    if isinstance(exc, (TypeError, AttributeError, KeyError, IndexError,
                        NameError, UnboundLocalError, ValueError,
                        AssertionError, ZeroDivisionError)):
        return 'internal'

    msg = str(exc).lower()
    tn = type(exc).__name__.lower()

    # Local request-memory admission is temporary server capacity pressure,
    # not an upstream model/key failure. Keep this import-free to avoid making
    # the error-envelope package depend on the cgroup implementation.
    if 'memorypressureerror' in tn:
        return 'server_busy'
    if 'all ' in msg and 'dispatch' in msg and 'attempts failed' in msg:
        return 'dispatch_exhausted'
    if 'no slot' in msg or 'no_slot' in msg:
        return 'no_slot'
    if 'endpointunreachable' in tn or 'endpoint unreachable' in msg or 'are unreachable' in msg:
        return 'endpoint_unreachable'
    # Billing exhaustion often arrives with HTTP 429 (and occasionally 403).
    # Strong quota markers must win before the generic status heuristics or the
    # UI will offer a futile retry instead of the billing/key recovery action.
    if (('insufficient' in msg and ('quota' in msg or 'balance' in msg))
            or 'insufficient_quota' in msg
            or 'credit_balance_too_low' in msg
            or 'exceeded your current quota' in msg):
        return 'quota'
    if 'timed out' in msg or 'timeout' in tn or 'timeout' in msg:
        return 'timeout'
    if '429' in msg or 'rate limit' in msg or 'rate-limit' in msg or 'too many requests' in msg:
        return 'ratelimit'
    if '401' in msg or '403' in msg or 'unauthorized' in msg or 'forbidden' in msg:
        return 'permission'
    if 'connectionerror' in tn or 'connection reset' in msg or 'connection aborted' in msg:
        return 'network'

    return 'generic'
