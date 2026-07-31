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

import json
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
    def __init__(self, msg='', *, is_quota=False, is_gateway=False, reason='',
                 status_code=0, is_shared_contention=False):
        super().__init__(msg)
        self.is_quota = bool(is_quota)
        # True when this is NOT a real per-key 429 but a gateway 5xx
        # (502/503/504) — or an upstream-vendor transient wrapped in a 4xx —
        # mapped onto the slot-rotation path. Real 429s reflect
        # per-key contention and rotate forever (a sibling key will free up);
        # a gateway/upstream storm means the WHOLE upstream is down, so the caller's
        # retry loop uses this to bound the outage instead of spinning forever
        # (see lib/llm_dispatch/api.py::_StreamRetryState gateway-outage cap).
        self.is_gateway = bool(is_gateway)
        # True when the body names a PROJECT-LEVEL limit shared with OTHER
        # tenants of the gateway account (2026-07-28: Moonshot "request
        # reached project (kimi-k3) TPM rate limit, current: 50.02M, limit:
        # 50M" — local traffic measured ~2M/min ≈ 4% of the pipe). This is
        # EXTERNAL contention, not key health: it must not feed the model
        # success-rate column nor the consecutive-429 auto-exhaust streak
        # (see Slot.record_error is_shared_contention branch).
        self.is_shared_contention = bool(is_shared_contention)
        self.reason = (reason or (str(msg) if msg else ''))[:200]
        # The real HTTP status that triggered this (429/402/502/…, or the
        # wrapped 4xx status for upstream-vendor transients). 0 = unknown.
        # The dispatch layer uses it for honest retry-HUD labels instead of
        # reporting everything as "Rate limited (429)".
        self.status_code = int(status_code or 0)


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


class BadRequestError(Exception):
    """HTTP 400 deterministic payload rejection (not any specific shape).

    Raised after every specific 400 matcher (token limit / image / prompt-
    too-long / stream-only / upstream-transient) fails — e.g. a vendor
    ``invalid_request_error``. The SAME payload fails identically on every
    key, so this says something about the PAYLOAD, not slot health: the
    dispatch layer releases the slot (no consecutive_errors → no 300s
    lockout, no key_stats feed — the ContentFilterError/InvalidImageError
    precedent) and pair-excludes so the remaining keys each get one try
    (a 400 CAN be key-specific — observed: an aliyun quota body that only
    one key rejects). Not raised for 401/403 (those have their own
    auth-vs-transient discrimination) or 5xx (retryable).
    """
    pass


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

# Phrases in a 4xx body that mean the failure is an UPSTREAM-VENDOR TRANSIENT,
# not a client/auth error — the gateway itself is telling us to retry later.
# Observed on the toio/sankuai gateway (2026-07-26): the claude-opus-5 vendor
# outage surfaced as HTTP 400 AND 403 with message
# "请求失败,请稍后(再)尝试" + ext.error.source=UPSTREAM_VENDOR, and our
# classifier treated the 403 as an auth failure (pair exclusion) and the 400
# as a non-retryable round-killer feeding the 300s consecutive-error lockout.
# Keep the list conservative: only phrases that unambiguously mean "wait and
# retry" — deterministic request-shape rejections ("Field required",
# "invalid …") must NEVER match, so bare 'try again' is deliberately absent.
_UPSTREAM_TRANSIENT_PATTERNS = [
    '请稍后', '请稍候', '稍后重试', '稍后再试', '稍后再尝试',
    'try again later',
    'temporarily unavailable',
    'overloaded',
    '负载较高', '负载高',
]

# Phrases in a 4xx body emitted by the gateway's own ROUTING layer when it
# fails to resolve a vendor group — a 503 wearing a 4xx costume, NOT an auth
# rejection. Observed on the toio/sankuai gateway (2026-07-26, 19:15–19:54):
# 22 hits of ``resolve groups failed: model unsupported by selected groups:
# claude-opus-5`` (HTTP 403) inside one 40-minute vendor-storm window, 10 of
# them misclassified PermissionError_ → task deaths / kimi fallbacks wearing
# the bogus "check your API key" envelope — while the SAME keys went on to
# serve 43 successful opus-5 rounds 19:55–21:20, minutes later. A real auth
# rejection does not heal in minutes. Pure-ASCII phrasing → encoding-stable
# (immune to the mojibake failure mode that killed the Chinese-phrase layer).
_GATEWAY_ROUTING_TRANSIENT_PATTERNS = [
    'resolve groups failed',
    'model unsupported by selected groups',
]

# Phrase emitted by the toio gateway's GENERIC upstream wrap: any upstream
# non-200 arrives as HTTP 400/403 with an Anthropic-style envelope whose
# error.type is literally ``<nil>`` and whose message is ``bad response
# status code <NNN> (request id: …)`` — no UPSTREAM_VENDOR marker, no ext
# tail, no retry-later phrasing, so it fell through every transient layer
# into BadRequestError (deterministic, pair-excluding round-killer).
# Observed 2026-07-28 12:47 on the Anthropic-native surface
# (/v1/anthropic/v1/messages): two identical adaptive+effort bodies 400'd
# inside one window, then the SAME shape went 200 in 8/8 samples minutes
# later — a real payload rejection does not heal, these are transient
# upstream blips. ASCII phrasing → encoding-stable (mojibake-proof), and a
# deterministic rejection ("Field required", "invalid …") never contains
# it, so rotating on it is safe: a genuinely bad payload still dies, just
# after bounded gateway-class rotation instead of fail-fast.
_WRAPPED_UPSTREAM_STATUS_PATTERNS = [
    'bad response status code',
]


def _is_upstream_vendor_transient(err_msg: str) -> bool:
    """True if a 4xx error body is an upstream-vendor TRANSIENT failure.

    Such errors must rotate slots (gateway-class retry), never exclude a
    (key, model) pair as auth, and never feed the consecutive-error lockout.

    Detection layers, in order:
      1. Encoding-stable marker: the gateway's own fault attribution
         ``"source":"UPSTREAM_VENDOR"`` in the ext tail. Pure ASCII, so it
         survives ANY mojibake / double-encoding of the human-readable
         message — the 2026-07-26 10:21 incident (task f8045792, round 41):
         a double-encoded toio 403 slipped past every Chinese phrase into
         ``PermissionError_`` (72h fallout: 34 permission-kind kimi
         fallbacks + 4 task deaths on 07-25 21:42 wearing the bogus
         "API Key 被拒绝 → Settings→Keys" envelope), while this marker
         sat intact in the same body. ``toio_api_error`` alone is NOT
         sufficient — it is the gateway's generic error type and a genuine
         auth 403 carries it too.
      2. Message phrases on the raw text (properly decoded bodies).
      3. Message phrases on the mojibake-REPAIRED text, for double-encoded
         bodies whose ext tail was truncated before the marker.
         ``repair_mojibake`` is conservative by design and refuses mixed-
         encoding strings — which is exactly why layer 1 must not rely on it.
    """
    if not err_msg:
        return False
    lower = err_msg.lower()
    if '"source":"upstream_vendor"' in lower.replace(' ', ''):
        return True
    if any(p in lower for p in _UPSTREAM_TRANSIENT_PATTERNS):
        return True
    if any(p in lower for p in _GATEWAY_ROUTING_TRANSIENT_PATTERNS):
        return True
    if any(p in lower for p in _WRAPPED_UPSTREAM_STATUS_PATTERNS):
        return True
    repaired = repair_mojibake(err_msg)
    if repaired is not err_msg:
        return any(p in repaired.lower() for p in _UPSTREAM_TRANSIENT_PATTERNS)
    return False


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
#  Error-body decoding / display
# ══════════════════════════════════════════════════════════

# Single cap for API error bodies in logs and in-memory error messages.
# Gateway error envelopes carry their diagnostic payload in the tail
# (``ext.error.source/service/stage`` + the request id the gateway needs for
# coordination), so the old 200/300/800-char caps amputated exactly the part
# worth reading — and the 800-char cap in classify_status_error cut the JSON
# mid-envelope, which also broke summarize_error_body's parse and leaked the
# raw envelope into the retry HUD. 4000 chars covers every realistic gateway
# envelope while still bounding a pathological body.
_ERR_BODY_LIMIT = 4000


def repair_mojibake(text: str) -> str:
    """Reverse a latin-1-misdecoded-UTF-8 string back to its intended text.

    The toio gateway's UPSTREAM_VENDOR wrap layer emits Chinese error text
    that was decoded as latin-1 somewhere upstream and then re-encoded as
    UTF-8 (verified from raw log bytes 2026-07-26: ``求失败，请稍后再尝试``
    arrived as ``æ±å¤±è´¥â¦`` — each real UTF-8 byte itself
    UTF-8-encoded). Our own decode is already UTF-8-correct, so the mojibake
    sails through verbatim into logs / the retry HUD. Repair it here, at the
    last boundary before display, CONSERVATIVELY: the repair is applied only
    when (1) the text encodes cleanly to latin-1/cp1252 (mojibake chars all
    live in U+0080–U+00FF + the cp1252 printable remaps), (2) those bytes
    decode cleanly as UTF-8, and (3) the result GAINS CJK the original did
    not have — proof the bytes were UTF-8 CJK all along. A legitimate
    "café" fails step 2 (0xE9 alone is invalid UTF-8) and is returned
    unchanged; proper Chinese already passes step 3's "no CJK before" gate.
    """
    if not text:
        return text
    # Fast gate: mojibake chars all live in U+0080–U+00FF (or the cp1252
    # printable remaps like U+201A). Without any of those, nothing to do.
    if not any('\u0080' <= ch <= '\u00ff' or ch == '\u201a' for ch in text):
        return text
    try:
        raw = text.encode('latin-1')
    except UnicodeEncodeError:
        # A cp1252 print layer upstream remaps control bytes to printable
        # chars (0x82 → U+201A); try that codec before giving up.
        try:
            raw = text.encode('cp1252')
        except UnicodeEncodeError as _e:
            logger.debug('repair mojibake: unencodable (%s)', _e)
            return text
    try:
        repaired = raw.decode('utf-8')
    except UnicodeDecodeError as _e:
        logger.debug('repair mojibake: undecodable (%s)', _e)
        return text
    _has_cjk = lambda s: any('\u4e00' <= ch <= '\u9fff' for ch in s)
    if _has_cjk(repaired) and not _has_cjk(text):
        return repaired
    return text


def decode_error_body(resp) -> str:
    """Decode a non-200 HTTP response body for error reporting.

    ``requests`` falls back to ISO-8859-1 for ``text/*`` responses without an
    explicit charset (the RFC 2616 default), which garbles UTF-8 CJK payloads
    from gateways that omit the charset header — observed on the sankuai toio
    gateway (2026-07-25): its Chinese 400 body surfaced as ``è¯·æ±...``
    mojibake in both logs and the frontend retry HUD. API error bodies are
    JSON per OpenAI/Anthropic convention and thus virtually always UTF-8, so
    decode UTF-8 first and fall back to the declared/apparent encoding on
    failure. Pure-ASCII bodies decode identically under both, so UTF-8-first
    is safe.
    """
    content = getattr(resp, 'content', b'') or b''
    if not content:
        return ''
    encoding = (getattr(resp, 'encoding', None) or '').lower().replace('_', '-')
    if encoding and encoding not in ('iso-8859-1', 'latin-1', 'latin1', 'ascii', 'utf-8'):
        try:
            return repair_mojibake(content.decode(encoding))
        except (LookupError, UnicodeDecodeError) as _e:
            logger.debug('decode error body: lookup failed/undecodable (%s)', _e)
            pass  # declared charset unusable — fall through to UTF-8
    try:
        return repair_mojibake(content.decode('utf-8'))
    except UnicodeDecodeError:
        apparent = getattr(resp, 'apparent_encoding', None) or 'utf-8'
        try:
            return repair_mojibake(content.decode(apparent, errors='replace'))
        except (LookupError, UnicodeDecodeError) as _e:
            logger.debug('decode error body: lookup failed/undecodable (%s)', _e)
            return repair_mojibake(content.decode('utf-8', errors='replace'))


_API_HTTP_PREFIX_RE = re.compile(r'^(API HTTP \d+:\s*)')


def summarize_error_body(text: str) -> str:
    """Extract the human-readable message from a JSON API error envelope.

    OpenAI and Anthropic share the ``{"error": {"message": ...}}`` envelope;
    surfacing the raw JSON in the retry HUD / error bubble is unreadable
    (nested quotes, provider boilerplate, mojibake amplification). Returns the
    ``error.message`` string with any leading ``API HTTP <code>:`` prefix
    preserved when the envelope parses; returns *text* unchanged otherwise —
    the raw form carries signal for the wrapped-overload / quota matchers.
    """
    if not text:
        return text or ''
    m = _API_HTTP_PREFIX_RE.match(text)
    prefix = m.group(1) if m else ''
    s = text[m.end():].strip() if m else text.strip()
    if not s.startswith('{'):
        return text
    try:
        data = json.loads(s)
    except ValueError as _e:
        logger.debug('summarize error body: unparseable (%s)', _e)
        return text
    err = data.get('error') if isinstance(data, dict) else None
    msg = err.get('message') if isinstance(err, dict) else None
    if isinstance(msg, str) and msg.strip():
        return prefix + msg
    return text


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


# Phrases naming a PROJECT-LEVEL limit shared with other tenants of the
# gateway account. BOTH must appear (narrow on purpose — owner 2026-07-28):
# a per-key/per-account throttle must NEVER match, or a genuinely sick key
# would be laundered into "external contention" and stop feeding the
# dead-key safety nets. Observed canonical body (Moonshot via sankuai):
#   "request reached project (kimi-k3) TPM rate limit, current: 50019215,
#    limit: 50000000"
_SHARED_PROJECT_LIMIT_PATTERNS = ('reached project', 'tpm rate limit')


def _is_shared_project_limit(err_msg: str) -> bool:
    """True if a 429 body names a shared PROJECT-level (TPM) limit.

    Such 429s are EXTERNAL contention — the pipe is saturated by other
    tenants of the gateway account, so rotating our own keys is futile
    (they all terminate at the same upstream project) and the error says
    nothing about key health.
    """
    if not err_msg:
        return False
    lower = err_msg.lower()
    return all(p in lower for p in _SHARED_PROJECT_LIMIT_PATTERNS)


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
        StreamOnlyError, BadRequestError, RetryableAPIError,
        or generic Exception.
    """
    # Lazy import to avoid a top-level cycle: lib.model_info is its own
    # module but lives in the same import graph and importing it here
    # keeps lib.llm_errors usable from anywhere without dragging in the
    # whole client.
    from lib.model_info import _learn_model_limit, _parse_token_limit_from_error

    # User-facing raise messages carry the extracted envelope message (clean,
    # readable in the retry HUD); err_msg stays RAW for the pattern matchers
    # below and for logs, which want the full provider context.
    display_msg = summarize_error_body(err_msg)

    if status_code == 429:
        # ★ Distinguish fatal billing 429s from transient rate-limit 429s.
        #   OpenAI returns HTTP 429 with code="insufficient_quota" for
        #   expired-balance keys — retrying on the same key is futile.
        if _is_quota_exhausted(err_msg):
            logger.warning('%s Quota exhausted (HTTP 429, persistent billing): %s',
                           log_prefix, err_msg[:_ERR_BODY_LIMIT])
            raise RateLimitError(display_msg, is_quota=True, reason=display_msg[:200],
                                 status_code=429)
        if _is_shared_project_limit(err_msg):
            # Project-LEVEL contention (shared gateway account saturated by
            # other tenants) — transient, NOT key health. Distinct from a
            # plain 429 only in how the dispatch layer ACCOUNTS for it.
            logger.info('%s Shared-project contention (HTTP 429, external): %s',
                        log_prefix, err_msg[:_ERR_BODY_LIMIT])
            raise RateLimitError(display_msg, status_code=429,
                                 is_shared_contention=True,
                                 reason=display_msg[:200])
        raise RateLimitError(display_msg, status_code=429)
    if status_code == 402:
        # ★ HTTP 402 Payment Required — DeepSeek and some providers return
        #   this for exhausted-balance keys. Treat identically to a quota-
        #   exhausted 429 so it hard-disables the key for the day.
        logger.warning('%s Payment required (HTTP 402): %s',
                       log_prefix, err_msg[:_ERR_BODY_LIMIT])
        raise RateLimitError(display_msg, is_quota=True, reason=display_msg[:200],
                             status_code=402)
    if status_code == 450:
        logger.warning('%s Content filter triggered (HTTP 450)', log_prefix)
        raise ContentFilterError(display_msg)
    if status_code in _PERMISSION_STATUS_CODES:
        # ★ A 401/403 whose body is an upstream-vendor TRANSIENT is NOT an
        #   auth failure (toio UPSTREAM_VENDOR wrap, 2026-07-26: the
        #   claude-opus-5 vendor outage returned HTTP 403 "请求失败,请稍后再
        #   尝试"). Treating it as one excluded the (key, model) pair and fed
        #   record_error(is_rate_limit=False) → the 300s consecutive-error
        #   lockout — while the truth was a sick vendor. Escalate as
        #   gateway-class so dispatch rotates slots (0.5s cooldown) instead.
        if _is_upstream_vendor_transient(err_msg):
            logger.warning('%s Upstream-vendor transient wrapped in HTTP %d '
                           '(NOT auth) — escalating to dispatch for rotation: %s',
                           log_prefix, status_code, err_msg[:_ERR_BODY_LIMIT])
            raise RateLimitError(display_msg, is_gateway=True,
                                 reason=f'HTTP {status_code}: {display_msg[:180]}',
                                 status_code=status_code)
        logger.warning('%s Permission error (HTTP %d)', log_prefix, status_code)
        raise PermissionError_(display_msg)
    if status_code == 413:
        logger.warning('%s Request entity too large (HTTP 413) — '
                       'treating as prompt-too-long: %s', log_prefix, err_msg[:_ERR_BODY_LIMIT])
        raise PromptTooLongError(display_msg)
    if status_code == 400:
        _detected_limit = _parse_token_limit_from_error(err_msg, model)
        if _detected_limit:
            _learn_model_limit(model, _detected_limit)
            raise ModelLimitError(display_msg, model, _detected_limit, max_tokens)
        if _is_image_error(err_msg):
            logger.warning('%s Image content error (HTTP 400): %s',
                           log_prefix, err_msg[:_ERR_BODY_LIMIT])
            raise InvalidImageError(display_msg)
        if _is_prompt_too_long(err_msg):
            logger.warning('%s Prompt too long detected (HTTP 400): %s',
                           log_prefix, err_msg[:_ERR_BODY_LIMIT])
            raise PromptTooLongError(display_msg)
        if _is_stream_only_error(err_msg):
            logger.warning('%s Model %s only supports stream mode — '
                           'non-streaming request rejected', log_prefix, model)
            raise StreamOnlyError(display_msg, model)
        # ★ Upstream-vendor transient wrapped as HTTP 400 (toio 2026-07-26:
        #   "请求失败,请稍后重试", ext.error.source=UPSTREAM_VENDOR). The
        #   specific matchers above (token limit / image / prompt / stream-
        #   only) already claimed the deterministic shapes; what remains here
        #   was a NON-RETRYABLE round-killer feeding the 300s consecutive-
        #   error slot lockout. It is transient by its own text — rotate.
        if _is_upstream_vendor_transient(err_msg):
            logger.warning('%s Upstream-vendor transient wrapped in HTTP 400 '
                           '— escalating to dispatch for rotation: %s',
                           log_prefix, err_msg[:_ERR_BODY_LIMIT])
            raise RateLimitError(display_msg, is_gateway=True,
                                 reason=f'HTTP 400: {display_msg[:180]}',
                                 status_code=400)
        # Every specific 400 shape claimed above failed → deterministic
        # payload rejection. Log the FULL envelope (the ext.error tail is
        # the diagnostic payload), then raise the typed error the dispatch
        # layer releases the slot for — this is the branch that used to
        # fall through to the generic non-retryable Exception and feed the
        # 300s consecutive-error slot lockout (43 locks in one yuju
        # claude-opus-5 incident, 2026-07-26).
        logger.error('%s Non-retryable API error (HTTP 400, deterministic): %s',
                     log_prefix, err_msg[:_ERR_BODY_LIMIT])
        raise BadRequestError(display_msg)
    if status_code in _GATEWAY_THROTTLE_STATUS:
        # ★ 502/503/504 from the gateway = upstream overload or transient
        #   backend failure. Treat identically to 429: bubble to dispatch
        #   layer, cooldown this slot 0.5s, rotate to another slot, retry
        #   indefinitely. Retrying on the SAME key is futile — another
        #   slot (different key/model/backend pool) is far more likely to
        #   succeed. See CLAUDE.md §10.1 for the approved change history.
        logger.warning('%s Gateway throttle (HTTP %d) — escalating to dispatch '
                       'layer for slot rotation: %s',
                       log_prefix, status_code, err_msg[:_ERR_BODY_LIMIT])
        raise RateLimitError(display_msg, is_gateway=True,
                             reason=f'HTTP {status_code}: {display_msg[:180]}',
                             status_code=status_code)
    if status_code in _RETRYABLE_STATUS_CODES:
        # ★ Detect wrapped overload / rate-limit inside a generic 500.
        #   Some gateways receive 429 or 529 from the model server but
        #   can't map it, so they wrap it as HTTP 500 with a body like:
        #     {"status":500,"data":"No matching constant for [529]"}
        #   Retrying on the same key is futile — escalate to dispatch.
        if status_code == 500 and _is_wrapped_overload(err_msg):
            logger.warning('%s Gateway wrapped overload/rate-limit in HTTP 500 '
                           '— escalating to dispatch layer: %s',
                           log_prefix, err_msg[:_ERR_BODY_LIMIT])
            raise RateLimitError(display_msg, status_code=500)
        raise RetryableAPIError(display_msg, status_code=status_code)
    logger.error('%s Non-retryable API error (HTTP %d): %s',
                 log_prefix, status_code, err_msg[:_ERR_BODY_LIMIT])
    raise Exception(display_msg)
