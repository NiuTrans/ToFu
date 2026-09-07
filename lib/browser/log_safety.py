"""Credential-safe projections for browser URLs and diagnostic text.

Browser URLs can be bearer capabilities: query strings, userinfo, fragments,
and even path segments may contain a signed download token.  Log sinks need
the origin for diagnosis, but never the capability itself.  ``url_for_log``
is the single fail-closed projection for that boundary; ``text_for_log`` also
scrubs URLs embedded by browser/network exception messages.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit


_URL_IN_TEXT_RE = re.compile(r'https?://[^\s\'"<>\\]+', re.IGNORECASE)


def url_for_log(value: object) -> str:
    """Return only a URL's HTTP(S) origin, never its bearer-capability parts."""
    raw = str(value or '').strip()
    if not raw:
        return '[empty-url]'
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return '[invalid-url]'
    if scheme not in {'http', 'https'} or not hostname:
        return f'{scheme}:[redacted]' if scheme else '[relative-url]'
    display_host = f'[{hostname}]' if ':' in hostname else hostname
    origin = f'{scheme}://{display_host}'
    if port is not None:
        origin += f':{port}'
    if parsed.path not in {'', '/'} or parsed.query or parsed.fragment \
            or parsed.username is not None or parsed.password is not None:
        return origin + '/…'
    return origin + '/'


def text_for_log(value: object, *, max_chars: int = 240) -> str:
    """Scrub embedded HTTP(S) URLs and control characters, then bound text."""
    text = _URL_IN_TEXT_RE.sub(
        lambda match: url_for_log(match.group(0)), str(value or ''))
    text = re.sub(r'[\x00-\x1f\x7f]+', ' ', text)
    return text[:max(0, int(max_chars))]


__all__ = ['text_for_log', 'url_for_log']
