"""Call-owned page and HTTP elapsed budgets for anonymous fetching.

Entries: page_budget owns a URL's fallback chain; request_budget wraps
do_request; remaining_request_budget checks body reads; request_timeout feeds
Requests. Separate ContextVars keep page admission apart from active HTTP
retry policy: a page scope alone never changes direct shared-session users.
FetchRetry is the existing pooled adapter's retry policy.
Uses only Requests/urllib3 public timeout and retry extension points. Socket
operations remain cooperative: this does not terminate an in-flight read.
"""

import time
from contextlib import contextmanager
from contextvars import ContextVar

from urllib3.exceptions import MaxRetryError, ResponseError
from urllib3.util import Retry, Timeout

# One shared limit covers connect, response-header and status failures.
FETCH_RETRY_LIMIT = 2

_REQUEST_DEADLINE = ContextVar('tofu_fetch_request_deadline', default=None)
_PAGE_DEADLINE = ContextVar('tofu_fetch_page_deadline', default=None)


class RequestBudgetExceeded(Exception):
    """Private stop signal; Requests would relabel an inner OSError connection failure."""


def remaining_request_budget():
    """Return the active HTTP allowance, or raise without admitting more work."""
    deadline = _REQUEST_DEADLINE.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RequestBudgetExceeded('Fetch HTTP request budget exhausted')
    return remaining


@contextmanager
def page_budget(seconds):
    """Bind a URL allowance; nonpositive values disable only this local cap."""
    deadline = time.monotonic() + seconds if seconds > 0 else None
    enclosing = _PAGE_DEADLINE.get()
    if enclosing is not None:
        deadline = min(deadline, enclosing) if deadline is not None else enclosing
    token = _PAGE_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _PAGE_DEADLINE.reset(token)


def page_budget_expired():
    """A cooperative admission check, without interrupting an active provider."""
    deadline = _PAGE_DEADLINE.get()
    return deadline is not None and time.monotonic() >= deadline


@contextmanager
def request_budget(seconds):
    deadline = time.monotonic() + seconds
    for enclosing in (_REQUEST_DEADLINE.get(), _PAGE_DEADLINE.get()):
        if enclosing is not None:
            deadline = min(deadline, enclosing)
    token = _REQUEST_DEADLINE.set(deadline)
    try:
        remaining_request_budget()
        yield
    finally:
        _REQUEST_DEADLINE.reset(token)


class _RequestTimeout(Timeout):
    """Recompute remaining time when urllib3 clones a timeout for a retry."""

    def __init__(self, connect, read):
        super().__init__(connect=connect, read=read, total=remaining_request_budget())

    def clone(self):
        return type(self)(connect=self._connect, read=self._read)

    @property
    def connect_timeout(self):
        timeout = super().connect_timeout
        remaining = remaining_request_budget()
        return min(timeout, remaining) if remaining is not None else timeout

    @property
    def read_timeout(self):
        timeout = super().read_timeout
        remaining = remaining_request_budget()
        return min(timeout, remaining) if remaining is not None else timeout


def request_timeout(timeout):
    return _RequestTimeout(connect=min(timeout, 8), read=timeout)


def _retry_delay(retry, response):
    if retry.respect_retry_after_header and response is not None:
        delay = retry.get_retry_after(response)
        if delay:
            return delay
    return retry.get_backoff_time()


class FetchRetry(Retry):
    """Let scoped pre-body failures use the existing total retry allowance.

    urllib3 classifies a timed-out proxy CONNECT reply as a read error even
    though the origin GET has not been sent. Both that case and a transient
    response-header timeout can use the original total retry count. A separate
    read=1 ceiling otherwise leaves one retry unused after two such failures.
    The deadline still rejects unaffordable waits and later attempts. Direct
    users of the shared session retain its configured read=0.
    """

    def increment(self, method=None, url=None, response=None, error=None,
                  _pool=None, _stacktrace=None):
        try:
            return self._increment_with_budget(method, url, response, error,
                                               _pool, _stacktrace)
        except RequestBudgetExceeded:
            if response is None:
                raise
            # Keep the response in urllib3's ownership on this exit too.
            # raise_on_status=False returns it for do_request to inspect/close.
            raise MaxRetryError(_pool, url, ResponseError('Retry exceeds fetch budget')) from None

    def _increment_with_budget(self, method, url, response, error, pool, stacktrace):
        remaining = remaining_request_budget()
        current = self
        if remaining is not None and not self.history and self.read == 0:
            current = self.new(read=FETCH_RETRY_LIMIT)
        following = Retry.increment(current, method, url, response, error,
                                    pool, stacktrace)
        if remaining is not None and response is not None:
            if _retry_delay(following, response) >= remaining_request_budget():
                # urllib3's raise_on_status=False returns the original response
                # when increment refuses a status retry. Keep HTTP status truth
                # instead of sleeping past the budget or relabeling it timeout.
                raise MaxRetryError(pool, url, ResponseError('Retry exceeds fetch budget'))
        return following

    def sleep(self, response=None):
        remaining = remaining_request_budget()
        if remaining is None:
            return super().sleep(response)
        delay = _retry_delay(self, response)
        if delay >= remaining:
            raise RequestBudgetExceeded('Fetch HTTP retry exceeds remaining budget')
        if delay:
            time.sleep(delay)
        remaining_request_budget()
