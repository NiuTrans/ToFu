"""tofu_search.fetch.readers — Site-specific "reader" handlers.

A *reader* turns a URL that the anonymous HTTP/Playwright pipeline cannot
usefully extract (JS-only social apps, login walls) into a clean text block by
hitting a public, no-login data endpoint instead of scraping the page shell.

This is the middle tier of the fetch policy:

    skip (never fetch)  →  reader-handled (public text endpoint)  →  normal fetch

Design mirrors :mod:`tofu_search.providers`: an extensible registry (domain →
handler), NOT an if/else chain, so new sites are added by registering another
:class:`SiteReader`. A reader ``matches`` a URL and ``read`` returns extracted
text or ``None`` (miss → the caller falls through to the normal pipeline / skip
policy). Readers own only their public data endpoint; they reuse
:func:`tofu_search.http_client.http_get` for transport — no duplicated request
logic.

The first reader is :class:`TwitterReader`, which reads a tweet/status URL via
the Twitter syndication endpoint (``cdn.syndication.twimg.com/tweet-result``).
That endpoint requires no login; its ``token`` query param is not
server-validated (a random string works), but we generate the canonical token
(Vercel react-tweet's algorithm) for forward-compatibility.
"""

from __future__ import annotations

import collections
import math
import re
import threading
from typing import List, Optional

from tofu_search.http_client import http_get
from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'SiteReader',
    'TwitterReader',
    'register_reader',
    'get_reader',
    'get_readers',
]


# ═══════════════════════════════════════════════════════
#  Reader interface + registry
# ═══════════════════════════════════════════════════════

class SiteReader:
    """Base class for a site-specific public-endpoint reader.

    Subclass and override :meth:`matches` and :meth:`read`. A reader must be
    cheap to construct and thread-safe (readers are shared process-wide).
    """

    name = 'base'

    def matches(self, url: str) -> bool:
        """Return True when this reader can handle ``url``."""
        return False

    def read(self, url: str, *, max_chars: Optional[int] = None,
             timeout: int = 15) -> Optional[str]:
        """Return extracted text for ``url``, or None on any miss/failure."""
        return None


_lock = threading.Lock()
_readers: List[SiteReader] = []


def register_reader(reader: SiteReader) -> None:
    """Append a reader to the registry (first-match-wins on lookup)."""
    with _lock:
        _readers.append(reader)
    logger.info('[Reader] registered %s', getattr(reader, 'name', reader))


def get_readers() -> List[SiteReader]:
    """Return a snapshot of the registered readers."""
    with _lock:
        return list(_readers)


def get_reader(url: str) -> Optional[SiteReader]:
    """Return the first registered reader whose ``matches(url)`` is True.

    Defended: a reader whose ``matches`` raises is skipped (logged at debug),
    so a buggy reader degrades to the normal pipeline rather than crashing it.
    """
    for reader in get_readers():
        try:
            if reader.matches(url):
                return reader
        except Exception as e:
            logger.debug('[Reader] %s.matches raised for %s: %s',
                         getattr(reader, 'name', reader), url[:80], e)
    return None


# ═══════════════════════════════════════════════════════
#  Twitter / X syndication reader
# ═══════════════════════════════════════════════════════

_SYNDICATION_URL = 'https://cdn.syndication.twimg.com/tweet-result'

# x.com / twitter.com status URL → tweet id. Accepts www./mobile. subdomains,
# /<user>/status/<id> and /i/web/status/<id> shapes, trailing junk tolerated.
_STATUS_RE = re.compile(
    r'^https?://(?:[\w-]+\.)*(?:twitter|x)\.com/'
    r'(?:[^/]+/status|i/web/status)/(?P<id>\d+)',
    re.IGNORECASE,
)

# The feature flags react-tweet sends; the endpoint wants them present.
_FEATURES = ';'.join([
    'tfw_timeline_list:',
    'tfw_follower_count_sunset:true',
    'tfw_tweet_edit_backend:on',
    'tfw_refsrc_session:on',
    'tfw_fosnr_soft_interventions_enabled:on',
    'tfw_show_birdwatch_pivots_enabled:on',
    'tfw_show_business_verified_badge:on',
    'tfw_duplicate_scribes_to_settings:on',
    'tfw_use_profile_image_shape_enabled:on',
    'tfw_show_blue_verified_badge:on',
    'tfw_legacy_timeline_sunset:true',
    'tfw_show_gov_verified_badge:on',
    'tfw_show_business_affiliate_badge:on',
    'tfw_tweet_edit_frontend:on',
])


def _js_number_to_string(val: float, radix: int) -> str:
    """Port of V8's ``Number.prototype.toString(radix)`` (ES5 §9.8.1).

    Needed to reproduce Vercel react-tweet's token generator byte-for-byte.
    Adapted from yt-dlp's ``jsinterp.js_number_to_string`` (Unlicense).
    """
    if math.isnan(val):
        return 'NaN'
    if val == 0:
        return '0'
    if math.isinf(val):
        return '-Infinity' if val < 0 else 'Infinity'

    ALPHABET = b'0123456789abcdefghijklmnopqrstuvwxyz.-'

    result: collections.deque = collections.deque()
    sign = val < 0
    val = abs(val)
    fraction, integer = math.modf(val)
    delta = max(math.nextafter(.0, math.inf), math.ulp(val) / 2)

    if fraction >= delta:
        result.append(-2)  # '.'
    while fraction >= delta:
        delta *= radix
        fraction, digit = math.modf(fraction * radix)
        result.append(int(digit))
        needs_rounding = fraction > 0.5 or (fraction == 0.5 and int(digit) & 1)
        if needs_rounding and fraction + delta > 1:
            for index in reversed(range(1, len(result))):
                if result[index] + 1 < radix:
                    result[index] += 1
                    break
                result.pop()
            else:
                integer += 1
            break

    integer, digit = divmod(int(integer), radix)
    result.appendleft(digit)
    while integer > 0:
        integer, digit = divmod(integer, radix)
        result.appendleft(digit)

    if sign:
        result.appendleft(-1)  # '-'

    return bytes(ALPHABET[d] for d in result).decode('ascii')


def _syndication_token(tweet_id: str) -> str:
    """Reproduce ``((id/1e15)*PI).toString(36).replace(/(0+|\\.)/g,'')``."""
    raw = _js_number_to_string((int(tweet_id) / 1e15) * math.pi, 36)
    return raw.translate(str.maketrans(dict.fromkeys('0.')))


def extract_tweet_id(url: str) -> Optional[str]:
    """Return the numeric tweet id from a status URL, or None if not one.

    A non-status x.com/twitter.com URL (home, profile, /search) returns None,
    so it falls through to the normal skip/fetch policy.
    """
    m = _STATUS_RE.match(url or '')
    return m.group('id') if m else None


def _format_one(tweet: dict) -> str:
    """Format a single tweet dict (author line + body) — no nested tweets."""
    user = tweet.get('user') or {}
    name = (user.get('name') or '').strip()
    screen = (user.get('screen_name') or '').strip()
    created = (tweet.get('created_at') or '').strip()
    text = (tweet.get('text') or '').strip()

    who = name
    if screen:
        who = f'{name} (@{screen})' if name else f'@{screen}'
    header = who or 'Unknown author'
    if created:
        header = f'{header} · {created}'
    return f'{header}\n\n{text}'.strip()


def parse_tweet_result(data: dict, url: str = '') -> Optional[str]:
    """Turn a ``tweet-result`` JSON payload into a clean text block.

    Handles tombstones (deleted) and empty (not-found) payloads → None. Inlines
    a parent (reply-to) or quoted tweet when present, since the syndication
    endpoint embeds them. Pure function — unit-tested against fixtures.
    """
    if not isinstance(data, dict) or not data:
        return None
    typename = data.get('__typename')
    if typename == 'TweetTombstone':
        logger.debug('[Reader:twitter] tombstone (deleted/unavailable) — %s', url[:80])
        return None
    # A valid tweet payload has text; guard against unexpected shapes.
    if not (data.get('text') or data.get('user')):
        logger.debug('[Reader:twitter] payload has no tweet text — %s', url[:80])
        return None

    parts = [_format_one(data)]

    parent = data.get('parent')
    if isinstance(parent, dict) and parent.get('text'):
        parts.append('— In reply to —\n' + _format_one(parent))

    quoted = data.get('quoted_tweet')
    if isinstance(quoted, dict) and quoted.get('text'):
        parts.append('— Quoting —\n' + _format_one(quoted))

    return '\n\n'.join(parts).strip() or None


class TwitterReader(SiteReader):
    """Read a tweet/status URL via the public Twitter syndication endpoint."""

    name = 'twitter'

    def matches(self, url: str) -> bool:
        return extract_tweet_id(url) is not None

    def read(self, url: str, *, max_chars: Optional[int] = None,
             timeout: int = 15) -> Optional[str]:
        tweet_id = extract_tweet_id(url)
        if not tweet_id:
            return None
        params = {
            'id': tweet_id,
            'lang': 'en',
            'features': _FEATURES,
            'token': _syndication_token(tweet_id),
        }
        try:
            resp = http_get(_SYNDICATION_URL, params=params, timeout=timeout)
        except Exception as e:
            logger.warning('[Reader:twitter] request failed id=%s — %s: %s',
                           tweet_id, url[:80], e)
            return None
        if not resp.ok:
            logger.info('[Reader:twitter] HTTP %d id=%s — %s',
                        resp.status_code, tweet_id, url[:80])
            return None
        try:
            data = resp.json()
        except Exception as e:
            logger.warning('[Reader:twitter] bad JSON id=%s — %s: %s',
                           tweet_id, url[:80], e)
            return None

        text = parse_tweet_result(data, url)
        if not text:
            logger.info('[Reader:twitter] MISS (tombstone/empty) id=%s — %s',
                        tweet_id, url[:80])
            return None
        logger.info('[Reader:twitter] HIT id=%s (%d chars) — %s',
                    tweet_id, len(text), url[:80])
        if max_chars and len(text) > max_chars:
            return text[:max_chars] + '\n[…truncated]'
        return text


def install_default_readers() -> None:
    """Register the built-in readers exactly once (idempotent)."""
    with _lock:
        if any(isinstance(r, TwitterReader) for r in _readers):
            return
    register_reader(TwitterReader())


# Register built-ins on import — a bare `import tofu_search` unlocks the
# reader tier with no host wiring required.
install_default_readers()
