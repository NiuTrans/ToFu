"""GitHub REST client for the channel adapter (docs/CHANNELS.md C8).

Responsibility: one owner's authenticated, serial requests against the GitHub
REST API: the pinned ``X-GitHub-Api-Version``, conditional requests
(``If-None-Match`` / ``If-Modified-Since`` → ``304``), ``Link`` header
pagination (pages are never constructed by hand), redirects that repeat the
same request on the same origin (a POST is never turned into a GET; the token
never leaves the configured origin), and typed refusals: ``401`` →
``GitHubAuthError``; a primary or secondary rate limit → ``GitHubRateLimited``
with the protocol's resume time (``retry-after``, ``x-ratelimit-reset``, or
the documented one-minute minimum). The token is read from its source per
request and appears only in the ``Authorization`` header: never in URLs,
logs, exception text or return values.

Entry points: ``GitHubClient``, ``GitHubResponse``, ``http_transport``,
``rate_limit_resume_ms``, ``iso_seconds``, ``parse_time_ms``. Dependencies:
``lib.http_transport.client`` (production transport only; tests inject a fake
transport, so no test reaches the network).
"""
from __future__ import annotations

import email.utils
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from lib.channels.model import GITHUB, ChannelError
from lib.channels.store import now_ms

API_VERSION = GITHUB['apiVersion']
DEFAULT_API_BASE_URL = GITHUB['defaultApiBaseUrl']
SECONDARY_RATE_LIMIT_WAIT_MS = int(GITHUB['secondaryRateLimitWaitS']) * 1000
_REDIRECT_STATUSES = frozenset({301, 302, 307, 308})
_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')


class GitHubError(ChannelError):
    """A GitHub refusal or transport failure. Messages never carry the token."""


class GitHubAuthError(GitHubError):
    def __init__(self, message: str = 'GitHub rejected the token (401)'):
        super().__init__('auth_failed', message)


class GitHubRateLimited(GitHubError):
    def __init__(self, resume_at_ms: int):
        super().__init__('rate_limited', 'GitHub rate limit reached')
        self.resume_at_ms = int(resume_at_ms)


class GitHubHTTPError(GitHubError):
    def __init__(self, status: int, message: str = ''):
        super().__init__(f'http_{status}', message or f'GitHub answered HTTP {status}')
        self.status = status


class GitHubUnavailable(GitHubError):
    def __init__(self, detail: str):
        super().__init__('unavailable', f'GitHub is unreachable ({detail})')


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str

    def json(self) -> Any:
        return json.loads(self.body.decode('utf-8')) if self.body else None

    def header(self, name: str) -> str:
        return str(self.headers.get(name.lower()) or '')

    @property
    def etag(self) -> str:
        return self.header('etag')

    @property
    def last_modified(self) -> str:
        return self.header('last-modified')

    @property
    def poll_interval_s(self) -> int | None:
        value = self.header('x-poll-interval')
        return int(value) if value.isdigit() else None

    @property
    def date_ms(self) -> int | None:
        return http_date_ms(self.header('date'))

    def link(self, rel: str) -> str:
        for url, name in _LINK_RE.findall(self.header('link')):
            if name == rel:
                return url
        return ''

    def message(self) -> str:
        """GitHub's error ``message`` field, when the body carries one."""
        try:
            value = self.json()
        except ValueError:
            return ''
        return str(value.get('message') or '') if isinstance(value, dict) else ''


Transport = Callable[[str, str, dict[str, str], 'bytes | None'], GitHubResponse]


def http_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> GitHubResponse:
    """Production transport: the shared proxy-aware client, redirects left to the caller."""
    from lib.http_transport.client import http_request
    response = http_request(method, url, headers=headers, data=body, allow_redirects=False)
    try:
        return GitHubResponse(response.status_code,
                              {key.lower(): value for key, value in response.headers.items()},
                              response.content, url)
    finally:
        response.close()


def iso_seconds(ms: int) -> str:
    """GitHub's ``since`` timestamp format (``YYYY-MM-DDTHH:MM:SSZ``, UTC, whole seconds)."""
    return datetime.fromtimestamp(int(ms) // 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_time_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def http_date_ms(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(email.utils.parsedate_to_datetime(value).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def rate_limit_resume_ms(response: GitHubResponse, now: int) -> int | None:
    """When a 403/429 is a rate limit, the earliest time GitHub allows a retry.

    Order of the REST rate-limit rules: ``retry-after`` seconds; else
    ``x-ratelimit-remaining: 0`` → ``x-ratelimit-reset`` (UTC epoch seconds);
    else a 429 or a secondary-rate-limit message → wait at least one minute.
    A plain 403 (permission, locked conversation) is not a rate limit. The
    reset is GitHub's clock, so the wait is measured against the response's
    ``Date`` and applied to this host's clock (a skewed local clock must not
    turn the wait into a retry loop).
    """
    if response.status not in (403, 429):
        return None
    retry_after = response.header('retry-after')
    if retry_after.isdigit():
        return now + int(retry_after) * 1000
    reset = response.header('x-ratelimit-reset')
    if response.header('x-ratelimit-remaining') == '0' and reset.isdigit():
        return now + max(0, int(reset) * 1000 - (response.date_ms or now))
    if response.status == 429 or 'rate limit' in response.message().lower():
        return now + SECONDARY_RATE_LIMIT_WAIT_MS
    return None


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


class GitHubClient:
    """Serial REST calls for one owner. ``token`` returns the secret per request."""

    def __init__(self, base_url: str, token: Callable[[], str], *,
                 transport: Transport | None = None, clock: Callable[[], int] = now_ms):
        self.base_url = base_url.rstrip('/')
        self._origin = _origin(self.base_url)
        self._token = token
        self._transport = transport or http_transport
        self._clock = clock

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                payload: dict[str, Any] | None = None, etag: str = '', if_modified_since: str = '',
                allow_missing: bool = False) -> GitHubResponse:
        """One request; ``path`` is API-relative or an absolute ``Link``/redirect URL."""
        url = path if path.startswith(('https://', 'http://')) else self.base_url + path
        if params:
            url += ('&' if '?' in url else '?') + urlencode(params)
        if _origin(url) != self._origin:
            raise GitHubHTTPError(0, 'Refusing to send the GitHub token to another origin')
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': API_VERSION,
                   'Authorization': f'Bearer {self._token()}'}
        if etag:
            headers['If-None-Match'] = etag
        if if_modified_since:
            headers['If-Modified-Since'] = if_modified_since
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        from requests.models import DEFAULT_REDIRECT_LIMIT
        for _ in range(DEFAULT_REDIRECT_LIMIT + 1):
            try:
                response = self._transport(method, url, headers, body)
            except GitHubError:
                raise
            except Exception as error:
                raise GitHubUnavailable(type(error).__name__) from None
            location = response.header('location')
            if response.status in _REDIRECT_STATUSES and location:
                # GitHub's REST guidance: repeat the request at the new location.
                url = urljoin(url, location)
                if _origin(url) != self._origin:
                    raise GitHubHTTPError(response.status, 'GitHub redirected to another origin')
                continue
            return self._checked(response, allow_missing)
        raise GitHubHTTPError(0, 'GitHub redirected too many times')

    def _checked(self, response: GitHubResponse, allow_missing: bool) -> GitHubResponse:
        if response.status in (200, 201, 204, 304):
            return response
        if response.status == 401:
            raise GitHubAuthError()
        resume = rate_limit_resume_ms(response, self._clock())
        if resume is not None:
            raise GitHubRateLimited(resume)
        if allow_missing and response.status in (404, 410):
            return response
        raise GitHubHTTPError(response.status, response.message())

    def get(self, path: str, **kwargs: Any) -> GitHubResponse:
        return self.request('GET', path, **kwargs)

    def pages(self, path: str, params: dict[str, Any], *, etag: str = '',
              if_modified_since: str = '', allow_missing: bool = False) -> Iterator[GitHubResponse]:
        """The first response (possibly 304/404), then every ``Link: rel="next"`` page."""
        response = self.request('GET', path, params=params, etag=etag,
                                if_modified_since=if_modified_since, allow_missing=allow_missing)
        yield response
        while response.status == 200 and response.link('next'):
            response = self.request('GET', response.link('next'))
            yield response


__all__ = ['API_VERSION', 'DEFAULT_API_BASE_URL', 'GitHubAuthError', 'GitHubClient', 'GitHubError',
           'GitHubHTTPError', 'GitHubRateLimited', 'GitHubResponse', 'GitHubUnavailable',
           'http_date_ms', 'http_transport', 'iso_seconds', 'parse_time_ms', 'rate_limit_resume_ms']
