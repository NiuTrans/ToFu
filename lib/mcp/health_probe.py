"""lib/mcp/health_probe.py — the standard MCP *credential* health-probe interface.

Transport health (a live subprocess + a good protocol ping) does NOT imply the
server's stored CREDENTIALS are still valid. A session cookie / OAuth token /
API key expires while the subprocess stays happily connected, so every real
tool call fails — yet the settings panel shows a green "connected" card. This
module defines the OPTIONAL, declarative contract that lets ANY MCP server
(curated catalog entry OR a user's custom ``mcp_servers.json`` entry) opt into
background credential verification, and the PURE classifier that interprets a
probe's result.

────────────────────────────────────────────────────────────────────────
THE CONTRACT — declare a ``health_probe`` on the server's config/entry
────────────────────────────────────────────────────────────────────────

    "health_probe": {
        "tool": "list_projects",     # REQUIRED — a cheap, READ-ONLY tool whose
                                     #   success depends ONLY on the credential.
        "args": {},                  # optional — args to pass (default {})
        "fail_patterns": [           # optional — case-insensitive substrings
            "session cookie has expired",   # that, if present in the result,
            "not authenticated"            # mean the credential is dead. These
        ],                           #   are ADDED to DEFAULT_CRED_FAIL_PATTERNS.
    }

Two levels of support, both zero-to-one line:

  * **Zero-config (compliant servers).** A server that follows the project's
    structured auth convention — emitting ``login_required: true`` (see
    ``.tofu/skills/mcp-tool-error-hint-pattern.md``) or a plain ``MCP Error:``
    with a standard auth phrase — is detected by DEFAULT_CRED_FAIL_PATTERNS /
    STRUCTURED_EXPIRED_MARKERS alone. Declaring ``{"tool": "whoami"}`` is
    enough; no ``fail_patterns`` needed.
  * **Free-text servers.** A server that returns auth errors as a *successful*
    result whose TEXT is an error string (e.g. Overleaf's ``list_projects``
    → "Error fetching projects … session cookie has expired") declares its
    specific ``fail_patterns`` to pin the phrases down.

Design rules (why it is shaped this way):

  * A RAISED probe (transport blip / timeout) is classified ``unknown``, NEVER
    ``expired`` — a transient error must not cry wolf about a still-valid
    credential. (The bridge, not this module, owns the try/except.)
  * Patterns are SPECIFIC phrases, and the probe ``tool`` should return terse
    output, so a legitimate result (e.g. a project literally named
    "unauthorized") is unlikely to false-positive. This mirrors the
    hope-mcp lesson: only auth-shaped results should flag.
  * The classifier is PURE (text in → verdict out) so it is trivially testable
    and reused identically by every caller.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'DEFAULT_CRED_FAIL_PATTERNS',
    'STRUCTURED_EXPIRED_MARKERS',
    'HEALTH_PROBE_SCHEMA',
    'validate_health_probe',
    'classify_probe_result',
]

# Case-insensitive substrings that, in a probe RESULT, indicate the stored
# credential is no longer valid. Kept SPECIFIC (multi-word phrases, not bare
# "login") so terse read-only probe output doesn't false-positive. EN + 中文
# variants mirror the project's central auth-expired convention
# (.tofu/skills/mcp-tool-error-hint-pattern.md).
DEFAULT_CRED_FAIL_PATTERNS: tuple[str, ...] = (
    'session expired',
    'session has expired',
    'session cookie has expired',
    'session may have expired',
    'not authenticated',
    'not logged in',
    'please login',
    'please log in',
    'authentication failed',
    'authentication required',
    'unauthorized',
    'unauthenticated',
    'token expired',
    'token has expired',
    'token is invalid',
    'invalid token',
    'invalid or expired',
    'credentials expired',
    'login required',
    # Chinese-localized CLIs / servers
    '登录已过期',
    '登录已失效',
    '会话已过期',
    '请重新登录',
    '请先登录',
    '未登录',
    '未授权',
    '认证失败',
    '身份验证失败',
)

# The project's STRUCTURED auth-expired convention: a compliant wrapper emits a
# result dict carrying ``login_required: true``. When the MCP result text is the
# JSON serialization of that dict, these markers detect it directly (a stronger
# signal than free-text phrase matching). Matched case-insensitively.
STRUCTURED_EXPIRED_MARKERS: tuple[str, ...] = (
    '"login_required": true',
    '"login_required":true',
    "'login_required': true",
)

# Machine-readable description of the contract, surfaced via the API so the
# capabilities endpoint / docs can advertise it without a hand-typed copy.
HEALTH_PROBE_SCHEMA: dict = {
    'fields': {
        'tool': {'type': 'string', 'required': True,
                 'doc': 'A cheap READ-ONLY tool whose success depends only on '
                        'the stored credential.'},
        'args': {'type': 'object', 'required': False, 'default': {},
                 'doc': 'Arguments passed to the probe tool.'},
        'fail_patterns': {'type': 'array<string>', 'required': False,
                          'doc': 'Case-insensitive substrings meaning the '
                                 'credential is dead. ADDED to the built-in '
                                 'default auth-phrase set.'},
    },
    'default_fail_patterns': list(DEFAULT_CRED_FAIL_PATTERNS),
    'structured_markers': list(STRUCTURED_EXPIRED_MARKERS),
}


def validate_health_probe(spec: object, *, server: str = '') -> dict | None:
    """Validate + normalize a declared ``health_probe`` spec.

    Returns a normalized ``{tool, args, fail_patterns}`` dict, or ``None`` when
    the spec is absent or malformed (logging a WARNING so a bad future
    registration gets actionable feedback rather than a silent no-op).

    Normalization: ``fail_patterns`` are lowercased and MERGED with
    DEFAULT_CRED_FAIL_PATTERNS (de-duplicated, order-stable); ``args`` defaults
    to ``{}``.

    Args:
        spec: The raw value of the entry's ``health_probe`` key.
        server: Server name, for log context.
    """
    if spec is None:
        return None
    if not isinstance(spec, dict):
        logger.warning('[MCP] health_probe for %r ignored: expected an object, '
                       'got %s', server or '?', type(spec).__name__)
        return None

    tool = spec.get('tool')
    if not isinstance(tool, str) or not tool.strip():
        logger.warning('[MCP] health_probe for %r ignored: missing/invalid '
                       'required "tool" field', server or '?')
        return None

    args = spec.get('args', {})
    if not isinstance(args, dict):
        logger.warning('[MCP] health_probe for %r: "args" is not an object '
                       '(%s) — defaulting to {}', server or '?',
                       type(args).__name__)
        args = {}

    raw_patterns = spec.get('fail_patterns', [])
    explicit: list[str] = []
    if isinstance(raw_patterns, (list, tuple)):
        for p in raw_patterns:
            if isinstance(p, str) and p.strip():
                explicit.append(p.lower())
    elif raw_patterns:
        logger.warning('[MCP] health_probe for %r: "fail_patterns" is not a '
                       'list (%s) — ignoring', server or '?',
                       type(raw_patterns).__name__)

    # Merge defaults + explicit, de-duplicated, defaults first (order-stable).
    merged: list[str] = []
    seen: set[str] = set()
    for p in list(DEFAULT_CRED_FAIL_PATTERNS) + explicit:
        if p not in seen:
            seen.add(p)
            merged.append(p)

    return {'tool': tool.strip(), 'args': dict(args), 'fail_patterns': merged}


def _snippet(text: str) -> str:
    """First non-empty line of the result, capped — a short non-secret tooltip."""
    if not text:
        return ''
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return text.strip()[:200]


def classify_probe_result(text: str, spec: dict) -> tuple[str, str]:
    """Classify a probe RESULT string into a credential-health verdict.

    PURE: no I/O, no exceptions. The caller owns running the tool and mapping a
    raised call to ``unknown``.

    Args:
        text: The flattened probe result (as returned by ``bridge.call_tool``).
        spec: A NORMALIZED spec from ``validate_health_probe`` (its
            ``fail_patterns`` already merged with the defaults).

    Returns:
        ``(status, detail)`` where status is ``'ok'`` or ``'expired'`` and
        detail is a short non-secret snippet (empty for ``'ok'``).
    """
    low = (text or '').lower()
    # Structured convention first — the strongest signal.
    if any(m in low for m in STRUCTURED_EXPIRED_MARKERS):
        return 'expired', _snippet(text)
    patterns = spec.get('fail_patterns') or DEFAULT_CRED_FAIL_PATTERNS
    if any(p in low for p in patterns):
        return 'expired', _snippet(text)
    return 'ok', ''
