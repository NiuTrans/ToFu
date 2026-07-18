"""lib/llm_errors.py — LLM API exception classes and HTTP error classification.

Extracted from the monolithic LLM client into a standalone module.
All names are re-exported from ``lib.llm`` for convenience.

Public exceptions
=================
- :class:`RetryableAPIError` — HTTP 5xx that can be retried on the same key
- :class:`RateLimitError` — HTTP 429/402/5xx that should rotate to a different key
- :class:`PermissionError_` — HTTP 401/403
- :class:`ContentFilterError` — HTTP 450
- :class:`AbortedError` — user-requested abort
- :class:`ModelLimitError` — HTTP 400 detecting auto-correctable token limit
- :class:`PromptTooLongError` — HTTP 400/413 indicating context overflow
- :class:`InvalidImageError` — HTTP 400 from image content rejection
- :class:`StreamOnlyError` — HTTP 400 from non-streaming on stream-only models

Public classifier
=================
- :func:`classify_http_error` — central dispatch (always raises)

Predicates
==========
- :func:`is_image_error`, :func:`is_prompt_too_long`,
  :func:`is_quota_exhausted`, :func:`is_wrapped_overload`,
  :func:`is_stream_only_error`
"""

import re

from requests.exceptions import ChunkedEncodingError, ConnectionError

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Exception classes
# ══════════════════════════════════════════════════════════

class RetryableAPIError(Exception):
    """HTTP 5xx from the API gateway — worth retrying on the same key."""
    def __init__(self, msg='', status_code=0):
        super().__init__(msg)
        self.status_code = status_code


class RateLimitError(Exception):
    """HTTP 429 — should NOT retry on the same key; bubble up to dispatch layer to switch keys.

    Attributes:
        is_quota: True when the 429 indicates a PERSISTENT billing/quota problem
            (e.g. OpenAI ``insufficient_quota``, DeepSeek ``Insufficient Balance``,
            Anthropic ``credit_balance_too_low``).  These are NOT transient — no
            amount of waiting will fix them, so the dispatch layer should mark
            the entire KEY as exhausted for the day instead of cycling to it
            again after a brief cooldown.
        reason: Short human-readable reason (first ~200 chars of the error body).
    """
    def __init__(self, msg='', *, is_quota=False, is_gateway=False, reason=''):
        super().__init__(msg)
        self.is_quota = bool(is_quota)
        # True when this is NOT a real per-key 429 but a gateway 5xx
        # (502/503/504) mapped onto the slot-rotation path. Real 429s reflect
        # per-key contention and rotate forever (a sibling key will free up);
        # a gateway 5xx storm means the WHOLE upstream is down, so the caller's
        # retry loop uses this to bound the outage instead of spinning forever
        # (see lib/llm_dispatch/api.py::_StreamRetryState gateway-outage cap).
        self.is_gateway = bool(is_gateway)
        self.reason = (reason or (str(msg) if msg else ''))[:200]


class PermissionError_(Exception):
    """HTTP 401/403 — should NOT retry on the same key; bubble up to dispatch layer to switch keys."""
    pass


class ContentFilterError(Exception):
    """HTTP 450 — content policy violation. Should NOT fallback to another model (same content = same filter)."""
    pass


class AbortedError(Exception):
    """User requested abort — stop all retries immediately."""
    pass


class ModelLimitError(Exception):
    """HTTP 400 indicating max_tokens exceeds model's limit — auto-learnable.

    Carries the detected limit so callers can auto-correct and retry.
    """
    def __init__(self, message, model, detected_limit, requested_limit):
        super().__init__(message)
        self.model = model
        self.detected_limit = detected_limit
        self.requested_limit = requested_limit


class PromptTooLongError(Exception):
    """HTTP 400 indicating the prompt/context exceeds the model's input limit.

    Triggers reactive compaction in the orchestrator — the conversation is
    compressed and the LLM call is retried automatically.
    """
    pass


class InvalidImageError(Exception):
    """HTTP 400 indicating image content is invalid (too large, corrupt, etc.).

    Same payload = same rejection on ALL keys/endpoints → should NOT retry.
    Bubbles up to the user with a descriptive message.
    """
    pass


class StreamOnlyError(Exception):
    """HTTP 400 indicating the model only supports stream mode.

    Should NOT retry on the same model — bubble up to dispatch layer to
    exclude this model and try a different one.
    """
    def __init__(self, message, model):
        super().__init__(message)
        self.model = model


class EndpointUnreachableError(Exception):
    """The model endpoint could not be reached at the connect phase.

    Raised when the TCP/TLS handshake to ``base_url`` times out or is
    refused (the host is down, the port isn't listening, or a firewall
    drops the SYN). Deliberately does NOT subclass ``ConnectionError`` so
    it is NOT swallowed by the same-key ``_RETRYABLE`` retry loop in
    ``stream_chat`` / ``chat`` / ``async_stream_chat`` — retrying a dead
    host on the same slot is futile and just burns the connect timeout
    over and over. Instead it escapes straight to the dispatch layer,
    which cools the slot down and fails over to a healthy one.

    Attributes:
        base_url: The endpoint that was unreachable (for the dispatch
            layer to cool down the matching slots + clear logging).
    """
    def __init__(self, message='', *, base_url=''):
        super().__init__(message)
        self.base_url = base_url or ''


def _is_connect_phase_error(exc: Exception) -> bool:
    """True if *exc* is a ``requests`` connect-phase failure (host unreachable).

    ``requests.exceptions.ConnectionError`` (and its ``ConnectTimeout``
    subclass) is raised when the socket can't be established at all — a
    distinct signal from a mid-stream reset (``ChunkedEncodingError``,
    raised later during body iteration) or a ``ReadTimeout`` (server
    accepted but is slow). Only the connect-phase case means "this
    endpoint is down; fail over".
    """
    from requests.exceptions import ConnectionError as _ReqConnError
    return isinstance(exc, _ReqConnError)


# ══════════════════════════════════════════════════════════
#  Pattern tables
# ══════════════════════════════════════════════════════════

# Patterns in HTTP 400 that indicate an image content error (not retryable)
_IMAGE_ERROR_PATTERNS = [
    'image dimensions exceed',
    'exceed max allowed size',
    'could not process image',
    'invalid image',
    'image is too large',
    'image resolution exceed',
]

# Patterns in HTTP 400 / SSE errors that indicate the prompt exceeds the model's input limit
_PROMPT_TOO_LONG_PATTERNS = [
    'prompt is too long', 'context length exceeded',
    'maximum context length', 'prompt too long',
    'input too long', 'exceeds the model',
    'token limit', 'context_length_exceeded',
    'max_prompt_tokens', 'request too large',
]

# Patterns that indicate a PERSISTENT quota / billing / balance exhaustion.
# These typically come back as HTTP 429 (OpenAI-style) or HTTP 402 (DeepSeek,
# Anthropic billing). Unlike transient RPM/TPM rate-limits, no amount of
# retrying or waiting will resolve them — the key needs a top-up.
#
# Keep the list conservative: only match phrases that UNAMBIGUOUSLY mean
# "pay more money", not phrases that could mean "wait a bit" (e.g. "rate
# limit exceeded", "requests per minute").
_QUOTA_EXHAUSTED_PATTERNS = [
    'insufficient_quota',           # OpenAI error code (billing)
    'insufficient quota',           # OpenAI (human text)
    'exceeded your current quota',  # OpenAI canonical message
    'check your plan and billing',  # OpenAI canonical message
    'insufficient_balance',         # DeepSeek error code
    'insufficient balance',         # DeepSeek / generic (human text)
    'credit_balance_too_low',       # Anthropic (billing)
    'credit balance is too low',    # Anthropic (human text)
    'billing_not_active',           # OpenAI billing suspension
    'account_deactivated',          # various
    'quota_exceeded',               # Azure / generic (billing context)
    'payment required',             # HTTP 402 literal
    'out of credits',               # various
    '余额不足',                       # DeepSeek / 国内服务商 (Chinese: insufficient balance)
    '额度不足',                       # Chinese: insufficient quota
    '余额为零',                       # Chinese: zero balance
    '欠费',                           # Chinese: in arrears
]


# ══════════════════════════════════════════════════════════
#  Status code sets
# ══════════════════════════════════════════════════════════

# Status codes that indicate a transient server-side issue (retry on same key).
# NOTE: 429 is NOT here — it gets RateLimitError which escapes to dispatch layer.
# NOTE: 502/503/504 are handled via _GATEWAY_THROTTLE_STATUS below (treated like
#   429 — slot rotation instead of same-key retry) since the gateway in this
#   project is stable and a 5xx almost always means upstream overload rather
#   than a real outage. See CLAUDE.md §10.1 change log.
_RETRYABLE_STATUS_CODES = {500, 529}

# Status codes that indicate gateway-side throttling / upstream overload.
# These are raised as RateLimitError so the dispatch layer rotates slots
# (0.5s cooldown + rotate) instead of burning 5 same-key retries with up
# to 24s exponential backoff. Effectively treats them identically to HTTP 429.
_GATEWAY_THROTTLE_STATUS = {502, 503, 504}

# Permission error status codes — escape immediately to dispatch layer
_PERMISSION_STATUS_CODES = {401, 403}

# Regex to detect embedded overload/rate-limit status codes in gateway error bodies.
# Matches patterns like: "No matching constant for [529]", "status_code: 429"
_WRAPPED_OVERLOAD_RE = re.compile(
    r'(?:'
    r'No matching constant for \[(?:429|529)\]'  # gateway can't map 429/529
    r'|"status"\s*:\s*(?:429|529)'               # JSON {"status": 529}
    r'|status[_\s]*code["\s:]*(?:429|529)'        # status_code: 429
    r')',
    re.IGNORECASE,
)

# Errors considered transient and worth retrying ON THE SAME KEY
_RETRYABLE = (ConnectionError, ChunkedEncodingError, BrokenPipeError,
              ConnectionResetError, RetryableAPIError)


# ══════════════════════════════════════════════════════════
#  Predicates
# ══════════════════════════════════════════════════════════

def _is_image_error(err_msg: str) -> bool:
    """Check if an HTTP 400 error is about invalid image content."""
    lower = err_msg.lower()
    return any(p in lower for p in _IMAGE_ERROR_PATTERNS)


def _is_prompt_too_long(err_msg: str) -> bool:
    """Check if an error message indicates the prompt exceeds model limits."""
    lower = err_msg.lower()
    return any(p in lower for p in _PROMPT_TOO_LONG_PATTERNS)


def _is_quota_exhausted(err_msg: str) -> bool:
    """Return True if *err_msg* indicates a persistent billing/quota problem.

    Used to distinguish fatal "this key is out of money" 429s from transient
    "slow down, try again" 429s. A quota-exhausted key should be disabled
    for the day (via the daily key-stats tracker), not just cooled down for
    0.5s and retried.
    """
    if not err_msg:
        return False
    lower = err_msg.lower()
    return any(p in lower for p in _QUOTA_EXHAUSTED_PATTERNS)


def _is_wrapped_overload(error_text: str) -> bool:
    """Detect if an HTTP 500 error body contains an embedded 429/529 overload.

    Some API gateways receive a 429 (rate limit) or 529 (overloaded) from
    the model server but cannot map it to a standard HTTP status, so they
    wrap it as a generic HTTP 500 with the original status in the body.
    Retrying on the same key is futile for overload — escalate to dispatch.
    """
    return bool(_WRAPPED_OVERLOAD_RE.search(error_text))


def _is_stream_only_error(error_text: str) -> bool:
    """Detect if an API error indicates the model only supports streaming.

    Recognizes error messages like:
      - "This model only support stream mode"
      - "please enable the stream parameter"
    """
    _lower = error_text.lower()
    return ('only support stream' in _lower
            or 'only supports stream' in _lower
            or 'enable the stream parameter' in _lower
            or 'stream mode only' in _lower)


# ══════════════════════════════════════════════════════════
#  Central HTTP error classifier
# ══════════════════════════════════════════════════════════

def _classify_http_error(status_code: int, err_msg: str, model: str,
                         log_prefix: str, *, max_tokens: int = 0) -> None:
    """Classify an HTTP error and raise the appropriate exception.

    Centralizes the error-classification chain shared by ``chat()`` and
    ``_stream_chat_once()``.  Always raises — never returns normally.

    Raises:
        RateLimitError, ContentFilterError, PermissionError_,
        PromptTooLongError, ModelLimitError, InvalidImageError,
        StreamOnlyError, RetryableAPIError, or generic Exception.
    """
    # Lazy import to avoid a top-level cycle: lib.model_info is its own
    # module but lives in the same import graph and importing it here
    # keeps lib.llm_errors usable from anywhere without dragging in the
    # whole client.
    from lib.model_info import _learn_model_limit, _parse_token_limit_from_error

    if status_code == 429:
        # ★ Distinguish fatal billing 429s from transient rate-limit 429s.
        #   OpenAI returns HTTP 429 with code="insufficient_quota" for
        #   expired-balance keys — retrying on the same key is futile.
        if _is_quota_exhausted(err_msg):
            logger.warning('%s Quota exhausted (HTTP 429, persistent billing): %s',
                           log_prefix, err_msg[:300])
            raise RateLimitError(err_msg, is_quota=True, reason=err_msg[:200])
        raise RateLimitError(err_msg)
    if status_code == 402:
        # ★ HTTP 402 Payment Required — DeepSeek and some providers return
        #   this for exhausted-balance keys. Treat identically to a quota-
        #   exhausted 429 so it hard-disables the key for the day.
        logger.warning('%s Payment required (HTTP 402): %s',
                       log_prefix, err_msg[:300])
        raise RateLimitError(err_msg, is_quota=True, reason=err_msg[:200])
    if status_code == 450:
        logger.warning('%s Content filter triggered (HTTP 450)', log_prefix)
        raise ContentFilterError(err_msg)
    if status_code in _PERMISSION_STATUS_CODES:
        logger.warning('%s Permission error (HTTP %d)', log_prefix, status_code)
        raise PermissionError_(err_msg)
    if status_code == 413:
        logger.warning('%s Request entity too large (HTTP 413) — '
                       'treating as prompt-too-long: %s', log_prefix, err_msg[:300])
        raise PromptTooLongError(err_msg)
    if status_code == 400:
        _detected_limit = _parse_token_limit_from_error(err_msg, model)
        if _detected_limit:
            _learn_model_limit(model, _detected_limit)
            raise ModelLimitError(err_msg, model, _detected_limit, max_tokens)
        if _is_image_error(err_msg):
            logger.warning('%s Image content error (HTTP 400): %s',
                           log_prefix, err_msg[:300])
            raise InvalidImageError(err_msg)
        if _is_prompt_too_long(err_msg):
            logger.warning('%s Prompt too long detected (HTTP 400): %s',
                           log_prefix, err_msg[:300])
            raise PromptTooLongError(err_msg)
        if _is_stream_only_error(err_msg):
            logger.warning('%s Model %s only supports stream mode — '
                           'non-streaming request rejected', log_prefix, model)
            raise StreamOnlyError(err_msg, model)
    if status_code in _GATEWAY_THROTTLE_STATUS:
        # ★ 502/503/504 from the gateway = upstream overload or transient
        #   backend failure. Treat identically to 429: bubble to dispatch
        #   layer, cooldown this slot 0.5s, rotate to another slot, retry
        #   indefinitely. Retrying on the SAME key is futile — another
        #   slot (different key/model/backend pool) is far more likely to
        #   succeed. See CLAUDE.md §10.1 for the approved change history.
        logger.warning('%s Gateway throttle (HTTP %d) — escalating to dispatch '
                       'layer for slot rotation: %.200s',
                       log_prefix, status_code, err_msg)
        raise RateLimitError(err_msg, is_gateway=True,
                             reason=f'HTTP {status_code}: {err_msg[:180]}')
    if status_code in _RETRYABLE_STATUS_CODES:
        # ★ Detect wrapped overload / rate-limit inside a generic 500.
        #   Some gateways receive 429 or 529 from the model server but
        #   can't map it, so they wrap it as HTTP 500 with a body like:
        #     {"status":500,"data":"No matching constant for [529]"}
        #   Retrying on the same key is futile — escalate to dispatch.
        if status_code == 500 and _is_wrapped_overload(err_msg):
            logger.warning('%s Gateway wrapped overload/rate-limit in HTTP 500 '
                           '— escalating to dispatch layer: %.200s',
                           log_prefix, err_msg)
            raise RateLimitError(err_msg)
        raise RetryableAPIError(err_msg, status_code=status_code)
    logger.error('%s Non-retryable API error (HTTP %d): %s',
                 log_prefix, status_code, err_msg[:300])
    raise Exception(err_msg)
