"""URL blocklist matching for the search/fetch/browser tool family.

Responsibility: the one rule for which URLs the assistant's web tools never
visit, so ``web_search`` listings, ``fetch_url``, the browser fetch fallback
and ``run_command`` URL transfers agree. The list is the existing
``search.skip_domains`` setting (``lib.SKIP_DOMAINS``); entries gained path
support without a new setting or knob.

Entry grammar (case-insensitive; an entry is matched against the URL it is
compared with after lower-casing both):

* ``example.com`` — a HOST rule. Matches that host exactly and every
  subdomain (``www.example.com``, ``a.b.example.com``), on either port.
  Historical behaviour compared the entry as a substring of ``netloc``,
  which made ``x.com`` also block ``dropbox.com`` and ``xn--x.com``;
  host matching is label-boundary aware now.
* ``example.com/private`` — a URL-PREFIX rule. Matches only URLs whose path
  starts with that path (whole prefix string, not a path-segment rule, so
  ``github.com/acme`` also covers ``github.com/acme-private``).
* ``example.com/*/name`` — a ``*`` path segment matches exactly one segment,
  which lets one entry cover the same repository or directory under every
  owner (``github.com/*/mirror``).
* ``example.com/**/name`` — a ``**`` segment matches any number of segments,
  including none, so a directory nested at any depth is covered.
* ``*`` inside a segment (``*mirror*``) matches any characters within that one
  segment, never across a ``/``; every other character is literal.
* ``*/<path pattern>`` — a ``*`` host matches every host. It needs a path
  pattern: a bare ``*`` is ignored rather than refusing every URL.

An entry with any ``*`` matches segment by segment and covers everything
below its last segment (``a/*/b`` also matches ``a/x/b/c``); an entry
without one keeps the whole-prefix-string meaning above.

A scheme (``https://``) is optional; a query or fragment never influences
matching. Blank entries, whitespace and trailing slashes are ignored. A host
rule with a port (``localhost:8080``) matches only that port.

Entry points: :func:`blocked_url` (the matched entry or ``None``) and
:func:`url_is_blocked`. Dependencies: standard library only — the packaged
copy (``tofu_search/generated_url_blocklist.py``) is exported by
``scripts/gen_packaged_search_runtime.py``; edit here and regenerate.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

__all__ = ['blocked_url', 'url_is_blocked']


def _parts(url: str):
    """(host, port, path) of an absolute HTTP(S) URL, else ``None``."""
    if not url:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    lowered = candidate.lower()
    if not lowered.startswith(('http://', 'https://')):
        # Only absolute HTTP(S) URLs carry this policy. ``//host/path`` and a
        # bare ``host/path`` are accepted for the entry-side callers; every
        # other scheme (mailto:, file:, ftp:) and every relative path is out
        # of scope, so a local file can never be refused as a web source.
        if '://' in candidate or lowered.startswith(('mailto:', 'file:', 'ftp:', 'data:', 'javascript:')):
            return None
    try:
        split = urlsplit(candidate if '://' in candidate else '//' + candidate)
    except ValueError:
        return None
    host = (split.hostname or '').lower().rstrip('.')
    if not host:
        return None
    try:
        port = split.port
    except ValueError:
        return None
    path = split.path or '/'
    return host, port, path.lower()


def _entry_parts(entry: str):
    """(host, port, path prefix, has wildcard segment) for a list entry."""
    text = str(entry or '').strip().lower()
    if not text:
        return None
    for prefix in ('https://', 'http://'):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if '://' in text:
        return None  # a non-HTTP scheme is not a URL this policy can refuse
    text = text.split('?', 1)[0].split('#', 1)[0].strip('/')
    if not text:
        return None
    host_part, _, path_part = text.partition('/')
    host = host_part.rstrip('.')
    port = None
    if ':' in host_part:
        host, _, port_text = host_part.rpartition(':')
        if port_text.isdigit():
            port = int(port_text)
        else:
            host = host_part
    if not host or (host == '*' and not path_part):
        return None
    return host, port, path_part, '*' in path_part


def _segment_matches(pattern: str, segment: str) -> bool:
    if pattern == '*':
        return bool(segment)
    if '*' not in pattern:
        return pattern == segment
    return re.fullmatch('.*'.join(map(re.escape, pattern.split('*'))), segment) is not None


def _segments_match(pattern: list[str], segments: list[str]) -> bool:
    """Whether ``pattern`` matches a leading run of ``segments``."""
    if not pattern:
        return True
    head, rest = pattern[0], pattern[1:]
    if head == '**':
        return any(_segments_match(rest, segments[start:]) for start in range(len(segments) + 1))
    return bool(segments) and _segment_matches(head, segments[0]) and _segments_match(rest, segments[1:])


def _path_matches(path: str, prefix: str, wildcard: bool) -> bool:
    if not prefix:
        return True
    if not wildcard:
        return path.startswith('/' + prefix)
    return _segments_match(prefix.split('/'), path.split('/')[1:])


def blocked_url(url: str, entries) -> str | None:
    """The first blocklist entry that refuses ``url``, or ``None``.

    ``entries`` is any iterable of strings in the grammar documented above.
    Non-HTTP(S) URLs (``file:``, ``mailto:``, relative paths) never match:
    this policy is about network reads, not local files.
    """
    parsed = _parts(url)
    if parsed is None:
        return None
    host, port, path = parsed
    for entry in entries or ():
        parts = _entry_parts(entry)
        if parts is None:
            continue
        entry_host, entry_port, prefix, wildcard = parts
        if entry_host != '*' and host != entry_host and not host.endswith('.' + entry_host):
            continue
        if entry_port is not None and entry_port != port:
            continue
        if _path_matches(path, prefix, wildcard):
            return str(entry)
    return None


def url_is_blocked(url: str, entries) -> bool:
    return blocked_url(url, entries) is not None
