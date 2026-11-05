"""Exception → ``kind`` classification for the error envelope.

Recognizes both the typed exceptions in ``lib.llm`` and the string-shaped
errors that bubble up from the dispatch layer.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# CPython's ``Thread.start()`` text when pthread_create fails with EAGAIN (a
# container pids limit, ``ulimit -u`` or thread-stack memory).
_CPYTHON_THREAD_START_REFUSAL = "can't start new thread"


def is_host_thread_refusal(exc: BaseException) -> bool:
    """True when the host refused a thread, raw or typed by the LLM transport."""
    from lib.llm.llm_errors import LocalRequestPreparationError
    if isinstance(exc, LocalRequestPreparationError):
        return exc.stage == 'thread_start'
    return (isinstance(exc, RuntimeError)
            and str(exc) == _CPYTHON_THREAD_START_REFUSAL)


def _classify_exception(exc: BaseException) -> str:
    """Map an exception to a `kind` string.

    Recognizes both the typed exceptions in ``lib.llm.llm_errors`` and the
    string-shaped errors that bubble up from the dispatch layer.
    """
    from lib.agent_core.context_errors import TurnRequirementsDeadlineError
    if isinstance(exc, TurnRequirementsDeadlineError):
        return 'timeout'
    # Local durability failures must win before upstream timeout heuristics.
    # Match the class ancestry without importing the task coordinator here.
    delivery_types = {cls.__name__ for cls in type(exc).__mro__}
    if 'EventDeliveryCancelled' in delivery_types or 'EventDeliveryRevoked' in delivery_types:
        return 'aborted'
    if 'EventDeliveryError' in delivery_types:
        return 'event_delivery_failed'
    # First try the typed-exception path (preferred).
    try:
        from lib.llm import (
            AbortedError as _Abort,
            BadRequestError as _BR,
            ContentFilterError as _CF,
            ContextCompactionError as _Compact,
            EndpointUnreachableError as _Unreach,
            InvalidImageError as _Img,
            LocalRequestPreparationError as _LocalPrepare,
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
        _Abort = _BR = _CF = _Compact = _Img = _LocalPrepare = _Mlim = _Perm = _Plong = _RL = _Req = _Retry = _SO = _Unreach = None  # type: ignore

    if _Abort is not None and isinstance(exc, _Abort):
        return 'aborted'
    from lib.llm.llm_errors import (
        ProviderToolCallFloodError, RequestTransportError,
        SubscriptionLoginRequiredError, SubscriptionCredentialUnavailableError,
    )
    if isinstance(exc, ProviderToolCallFloodError):
        return 'tool_loop'
    if isinstance(exc, SubscriptionCredentialUnavailableError):
        return 'generic'
    if isinstance(exc, SubscriptionLoginRequiredError):
        return 'oauth_reauth'
    if isinstance(exc, RequestTransportError):
        return exc.error_kind
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
    if _Req is not None and isinstance(exc, _Req):
        # 404 (model/route missing upstream) is NOT a payload rejection —
        # keeping it inside bad_request rendered every 404 under the
        # hardcoded "HTTP 400" title. Protocol-defect subclasses
        # (ContinueToolHistory/ResumeState) carry status_code=0 and keep
        # the bad_request bucket.
        if getattr(exc, 'status_code', 0) == 404:
            return 'not_found'
        return 'bad_request'
    if _BR is not None and isinstance(exc, _BR):
        # A gateway's protocol-unsupported 404 is route-missing evidence for
        # dispatch (ModelRouteMissingError), yet still an HTTP 404 upstream.
        if getattr(exc, 'status_code', 0) == 404:
            return 'not_found'
        return 'bad_request'
    if _Perm is not None and isinstance(exc, _Perm):
        return 'permission'
    if _CF is not None and isinstance(exc, _CF):
        return 'content_filter'
    if _Img is not None and isinstance(exc, _Img):
        return 'invalid_image'
    if _LocalPrepare is not None and isinstance(exc, _LocalPrepare):
        # Preparation keeps neutral dispatch settlement, while the typed cause
        # preserves the user recovery action instead of suggesting log inspection.
        if isinstance(exc.__cause__, SubscriptionCredentialUnavailableError):
            return 'generic'
        if isinstance(exc.__cause__, SubscriptionLoginRequiredError):
            return 'oauth_reauth'
        return 'internal'
    if _Compact is not None and isinstance(exc, _Compact):
        return 'internal'
    if _Plong is not None and isinstance(exc, _Plong):
        return 'prompt_too_long'
    if _SO is not None and isinstance(exc, _SO):
        return 'stream_only'
    if _Mlim is not None and isinstance(exc, _Mlim):
        return 'model_limit'
    # Type identity outranks the name/message heuristics below: a deadline
    # stop such as TranslationDeadlineExhausted names neither "timeout" nor
    # "timed out", and its last_err may quote an incidental "HTTP 429".
    if isinstance(exc, TimeoutError):
        return 'timeout'
    # A local resource refusal, not an upstream failure: the message matches
    # none of the provider heuristics below and used to read as 'generic'.
    if is_host_thread_refusal(exc):
        return 'internal'

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

    # The storage layer's typed failure is neither an upstream LLM error nor
    # a programming bug; its stable code carries the real taxonomy. Without
    # this branch a writer-starvation timeout surfaced as kind=generic —
    # titled "LLM call failed" — hiding the actual subsystem from the user.
    # Keep this import-free to avoid initializing the storage supervisor.
    #
    # `database_busy` / `database_unavailable` are STORAGE-layer failures, not
    # local server capacity: they own the storage authority (sidecar down,
    # fenced, saturated). They used to classify as `server_busy`, whose hint
    # blames concurrent-task slots and memory headroom — a dead end for an
    # operator whose real recovery is a server restart (2026-10-22). Event
    # failures keep their own event-delivery classification.
    if 'storageerror' in tn:
        code = str(getattr(exc, 'code', '') or '')
        if code in ('database_busy', 'database_unavailable'):
            return 'storage_unavailable'
        if code == 'database_timeout':
            return 'timeout'
        return 'internal'

    if 'all ' in msg and 'dispatch' in msg and 'attempts failed' in msg:
        return 'dispatch_exhausted'
    # OAuth-subscription session expired / logged out. These phrases carry no
    # "401"/"unauthorized" marker, so they previously fell to 'generic' and
    # told the user to "check the logs" — the exact dead-end from the 2026-09-23
    # incident (Codex refresh_token_invalidated surfaced as an opaque 500).
    # Match before permission/quota so a "sign-in required" never reads as a
    # key/quota fault. Recovery is a re-login, so it is its own kind.
    if ('not logged in' in msg or 'sign-in required' in msg
            or 'sign in required' in msg or 'sign in again' in msg
            or 'refresh is unavailable' in msg
            or 'refresh_invalidated' in msg
            or 'no valid oauth token' in msg
            or ('oauth' in msg and 'expired' in msg)):
        return 'oauth_reauth'
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
