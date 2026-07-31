"""lib/mcp/transport.py — Transport classification + header templating.

Three concerns that were previously implicit (and wrong once a third
transport existed) now have one home:

1. **Classification.** ``is_stdio`` / ``is_remote``. Callers used to write
   ``transport != 'sse'`` to mean "this is stdio" — a two-valued assumption
   that silently mis-classifies every transport added later.

2. **Credential templating.** A remote MCP server authenticates either with a
   header (``Authorization: Bearer <key>``) or with a **query parameter**
   (Amap: ``https://mcp.amap.com/mcp?key=<key>``). Neither ``headers`` nor
   ``url`` may hold the secret itself: both are *templates* holding
   ``${VAR}`` references, and the value comes from the server's existing
   ``env`` block, which is the single redacted credential store.

3. **Redaction.** One function decides what a config is allowed to show an
   API caller, so a new secret-bearing field can never be added to the
   config shape without this module having an opinion about it. Note that
   ``url`` is NOT safe to pass through verbatim — a query-string credential
   would leak through every config-returning endpoint.
"""

from __future__ import annotations

import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

# ── Transport vocabulary ─────────────────────────────────────────────
STDIO = 'stdio'
SSE = 'sse'
STREAMABLE_HTTP = 'streamable-http'

# Remote transports speak HTTP and are configured by ``url`` (+ optional
# ``headers``); stdio spawns a subprocess and is configured by ``command``.
REMOTE_TRANSPORTS = frozenset({SSE, STREAMABLE_HTTP})
VALID_TRANSPORTS = frozenset({STDIO}) | REMOTE_TRANSPORTS

# Accepted spellings for the streamable-HTTP transport. Upstream clients are
# inconsistent (Claude CLI writes ``http``, Cursor/Codex write
# ``streamable-http``, some docs write ``streamable_http``) and users
# copy-paste those blocks verbatim, so normalize rather than reject.
_TRANSPORT_ALIASES = {
    'http': STREAMABLE_HTTP,
    'https': STREAMABLE_HTTP,
    'streamable_http': STREAMABLE_HTTP,
    'streamablehttp': STREAMABLE_HTTP,
}

_PLACEHOLDER_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


def normalize_transport(cfg: dict) -> str:
    """Return the canonical transport name for ``cfg`` (default ``stdio``)."""
    raw = str((cfg or {}).get('transport') or STDIO).strip().lower()
    return _TRANSPORT_ALIASES.get(raw, raw)


def is_stdio(cfg: dict) -> bool:
    """True when this server is launched as a local subprocess.

    Anything not recognised as a remote transport is treated as stdio, which
    matches the config default and keeps an unknown/typo'd transport failing
    with the explicit "stdio transport requires command" error rather than
    silently attempting a URL-less HTTP connection.
    """
    return normalize_transport(cfg) not in REMOTE_TRANSPORTS


def is_remote(cfg: dict) -> bool:
    """True when this server is reached over HTTP (sse / streamable-http)."""
    return not is_stdio(cfg)


def stdio_command(cfg: dict) -> str:
    """The launcher command, or ``''`` for remote servers.

    Every caller that wants "the subprocess launcher, if there is one" should
    use this instead of re-deriving it from a transport comparison.
    """
    return (cfg or {}).get('command', '') if is_stdio(cfg) else ''


def _resolve_placeholders(raw: str, env: dict, *, server_name: str,
                          missing: list[str]) -> str:
    """Expand ``${VAR}`` in one string, appending unresolved names to ``missing``."""
    def _sub(m: re.Match) -> str:
        var = m.group(1)
        val = env.get(var) or os.environ.get(var) or ''
        if not str(val).strip():
            missing.append(var)
            return ''
        return str(val)
    return _PLACEHOLDER_RE.sub(_sub, str(raw))


def _raise_missing(server_name: str, missing: list[str], what: str) -> None:
    raise ValueError(
        f'MCP server {server_name!r}: {what} credential(s) not set: '
        f'{", ".join(sorted(set(missing)))}. Add the value in '
        f'Settings → MCP so it is stored in the server\'s env block.'
    )


def resolve_url(cfg: dict, *, server_name: str = '') -> str:
    """Expand ``${VAR}`` placeholders in the endpoint URL.

    Some vendors authenticate by query parameter rather than by header (Amap:
    ``https://mcp.amap.com/mcp?key=${AMAP_MAPS_API_KEY}``). Templating the URL
    the same way headers are templated keeps ALL credentials in ``env`` — the
    one store that is redacted on the way out.

    The returned string may carry a real secret and MUST NOT be logged or
    returned to an API caller.
    """
    raw = (cfg or {}).get('url') or ''
    if not raw:
        return ''
    env = (cfg or {}).get('env') or {}
    missing: list[str] = []
    resolved = _resolve_placeholders(raw, env, server_name=server_name,
                                    missing=missing)
    if missing:
        _raise_missing(server_name, missing, 'endpoint URL')
    return resolved


def resolve_headers(cfg: dict, *, server_name: str = '') -> dict[str, str]:
    """Expand ``${VAR}`` placeholders in the header template.

    Values resolve from the server's own ``env`` block first (the redacted
    credential store the settings UI already writes), then from the process
    environment. The returned dict carries real secrets and MUST NOT be
    logged or returned to an API caller.

    Raises:
        ValueError: a placeholder has no value. Sending the literal
            ``${VAR}`` upstream would surface as an opaque 401 from the
            vendor; failing here names the missing key instead.
    """
    template = (cfg or {}).get('headers') or {}
    if not isinstance(template, dict):
        raise ValueError(
            f'MCP server {server_name!r}: "headers" must be an object, '
            f'got {type(template).__name__}'
        )

    env = (cfg or {}).get('env') or {}
    resolved: dict[str, str] = {}
    missing: list[str] = []

    for key, raw in template.items():
        resolved[str(key)] = _resolve_placeholders(
            raw, env, server_name=server_name, missing=missing)

    if missing:
        _raise_missing(server_name, missing, 'header')

    if resolved:
        logger.debug('[MCP:Transport] %s resolved %d auth header(s): %s',
                     server_name or '?', len(resolved), sorted(resolved))
    return resolved


# ══ Config field exposure classification (FAIL-CLOSED WHITELIST) ══════
#
# Redaction used to be a BLACKLIST — ``{k: v for k, v in cfg.items() if k !=
# 'env'}``. Every new credential-bearing field then had to be remembered and
# added by hand, and three were missed in a row (``env`` → ``headers`` →
# ``url``). A blacklist is only as good as the author's memory at the moment
# they add a field, so the default for anything unforeseen was EXPOSE.
#
# The default is now DROP. A field reaches an API caller only if it is
# explicitly classified below, so a future field is silently withheld rather
# than silently leaked, and ``test_mcp_remote_transport`` fails until someone
# states its exposure level.

#: Echoed verbatim — structural, never carries a credential.
PUBLIC_CONFIG_FIELDS = frozenset({
    'command', 'args', 'transport', 'enabled', 'description', 'timeout',
})

#: Echoed only after a field-specific transform (see ``redact_config``).
TRANSFORMED_CONFIG_FIELDS = frozenset({'url', 'headers'})

#: Never echoed in any form. ``env`` is the credential store; callers learn
#: which KEYS exist via ``stored_env_keys``, never their values.
SECRET_CONFIG_FIELDS = frozenset({'env'})

#: Every field whose exposure level has been decided. The ratchet test
#: asserts ``MCPServerConfig`` declares nothing outside this set.
CLASSIFIED_CONFIG_FIELDS = (
    PUBLIC_CONFIG_FIELDS | TRANSFORMED_CONFIG_FIELDS | SECRET_CONFIG_FIELDS
)


def redact_config(cfg: dict) -> dict:
    """Project a stored server config down to what an API caller may see.

    Fail-closed: a field is emitted ONLY when classified above. An
    unclassified field (hand-added to ``mcp_servers.json``, or a new field
    someone forgot to classify) is dropped and logged, because the cost of
    hiding a harmless field is a missing UI detail while the cost of echoing
    a credential-bearing one is a leaked secret.

    ``url`` and ``headers`` are credential CARRIERS by contract (query-param
    and header auth respectively), so both go through a masking transform
    rather than being copied.
    """
    src = cfg or {}
    out: dict = {}
    unknown: list[str] = []

    for key, value in src.items():
        if key in PUBLIC_CONFIG_FIELDS:
            out[key] = value
        elif key in SECRET_CONFIG_FIELDS:
            continue
        elif key in TRANSFORMED_CONFIG_FIELDS:
            continue          # emitted below, after transformation
        else:
            unknown.append(key)

    if unknown:
        logger.warning(
            '[MCP:Transport] withheld unclassified config field(s) %s from an '
            'API response — classify them in lib/mcp/transport.py '
            '(PUBLIC_CONFIG_FIELDS / TRANSFORMED_CONFIG_FIELDS / '
            'SECRET_CONFIG_FIELDS)', sorted(unknown),
        )

    template = src.get('headers') or {}
    if isinstance(template, dict) and template:
        out['headers'] = {
            str(k): _PLACEHOLDER_RE.sub(lambda m: '${%s}' % m.group(1),
                                        _mask_literal(str(v)))
            for k, v in template.items()
        }
    url = src.get('url')
    if url:
        out['url'] = redact_url(str(url))
    return out


def redact_url(url: str) -> str:
    """Mask query-parameter VALUES in an endpoint URL, keeping its shape.

    A vendor that authenticates by query string (Amap ``?key=<secret>``) would
    otherwise leak the credential through every endpoint that echoes a server
    config. Placeholder references survive as-is because they are not secrets;
    any other value becomes ``***``. The scheme/host/path stay intact so the
    UI can still show which endpoint a server talks to.
    """
    base, sep, query = str(url).partition('?')
    if not sep or not query:
        return base
    parts = []
    for pair in query.split('&'):
        key, eq, value = pair.partition('=')
        if not eq:
            parts.append(key)
            continue
        if _PLACEHOLDER_RE.fullmatch(value.strip()):
            parts.append(f'{key}={value}')
        else:
            parts.append(f'{key}=***' if value else f'{key}=')
    return f'{base}?{"&".join(parts)}'


def scrub_text(text: str) -> str:
    """Mask credentials in ANY free text before it reaches a log sink.

    The third credential exit (after the two config-returning endpoints) is
    logging. A remote connect failure surfaces as an httpx error whose message
    embeds the RESOLVED request URL — ``... for url
    'https://mcp.amap.com/mcp?key=<real key>'`` — so a single failed handshake
    writes a live credential into app.log / error.log, where it persists far
    longer than an API response and is far harder to clean up.

    Applied at the point where transport text is rendered into a log record or
    a user-facing error, so every diagnostic path is covered by one call
    rather than each site remembering to mask.
    """
    if not text:
        return text
    return _URL_IN_TEXT_RE.sub(lambda m: redact_url(m.group(0)), str(text))


#: Matches an http(s) URL embedded in free text, up to the first character
#: that cannot appear in one. Trailing quotes/parens are excluded so the
#: surrounding message punctuation survives the substitution.
_URL_IN_TEXT_RE = re.compile(r'https?://[^\s\'"<>\\]+')


def _mask_literal(value: str) -> str:
    """Replace any non-placeholder text in a header value with a mask."""
    parts = _PLACEHOLDER_RE.split(value)
    # re.split with one group yields [text, var, text, var, ..., text]
    rebuilt = []
    for i, chunk in enumerate(parts):
        if i % 2 == 1:               # captured variable name
            rebuilt.append('${%s}' % chunk)
        elif chunk:
            # Keep a short structural hint (e.g. "Bearer ") but never a secret.
            rebuilt.append(chunk if len(chunk) <= 8 and ' ' in chunk else '***')
    return ''.join(rebuilt)


def header_env_keys(cfg: dict) -> list[str]:
    """Env var names referenced by the header template OR the endpoint URL.

    Drives the settings UI's "which credentials does this server need?"
    prompt, so it must cover BOTH credential carriers — a query-param vendor
    like Amap has an empty header block and would otherwise look credential-free.
    """
    keys: set[str] = set()
    template = (cfg or {}).get('headers') or {}
    if isinstance(template, dict):
        for raw in template.values():
            keys.update(_PLACEHOLDER_RE.findall(str(raw)))
    keys.update(_PLACEHOLDER_RE.findall(str((cfg or {}).get('url') or '')))
    return sorted(keys)


__all__ = [
    'STDIO', 'SSE', 'STREAMABLE_HTTP', 'REMOTE_TRANSPORTS', 'VALID_TRANSPORTS',
    'PUBLIC_CONFIG_FIELDS', 'TRANSFORMED_CONFIG_FIELDS',
    'SECRET_CONFIG_FIELDS', 'CLASSIFIED_CONFIG_FIELDS',
    'normalize_transport', 'is_stdio', 'is_remote', 'stdio_command',
    'resolve_headers', 'resolve_url', 'redact_config', 'redact_url',
    'scrub_text', 'header_env_keys',
]
