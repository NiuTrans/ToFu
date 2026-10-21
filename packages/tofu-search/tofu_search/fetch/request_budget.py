"""Call-owned HTTP retry and socket-phase budgets for anonymous fetching.

Entries: request_budget wraps do_request; request_timeout feeds Requests;
FetchRetry is the existing pooled adapter's retry policy. One ContextVar binds
the deadline to the synchronous request without mutating shared sessions.
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


class RequestBudgetExceeded(Exception):
    """Private stop signal; Requests would relabel an inner OSError connection failure."""


def _remaining():
    deadline = _REQUEST_DEADLINE.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RequestBudgetExceeded('Fetch HTTP request budget exhausted')
    return remaining


@contextmanager
def request_budget(seconds):
    deadline = time.monotonic() + seconds
    enclosing = _REQUEST_DEADLINE.get()
    if enclosing is not None:
        deadline = min(deadline, enclosing)
    token = _REQUEST_DEADLINE.set(deadline)
    try:
        _remaining()
        yield
    finally:
        _REQUEST_DEADLINE.reset(token)


class _RequestTimeout(Timeout):
    """Recompute remaining time when urllib3 clones a timeout for a retry."""

    def __init__(self, connect, read):
        super().__init__(connect=connect, read=read, total=_remaining())

    def clone(self):
        return type(self)(connect=self._connect, read=self._read)

    @property
    def connect_timeout(self):
        timeout = super().connect_timeout
        remaining = _remaining()
        return min(timeout, remaining) if remaining is not None else timeout

    @property
    def read_timeout(self):
        timeout = super().read_timeout
        remaining = _remaining()
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
        remaining = _remaining()
        current = self
        if remaining is not None and not self.history and self.read == 0:
            current = self.new(read=FETCH_RETRY_LIMIT)
        following = Retry.increment(current, method, url, response, error,
                                    pool, stacktrace)
        if remaining is not None and response is not None:
            if _retry_delay(following, response) >= _remaining():
                # urllib3's raise_on_status=False returns the original response
                # when increment refuses a status retry. Keep HTTP status truth
                # instead of sleeping past the budget or relabeling it timeout.
                raise MaxRetryError(pool, url, ResponseError('Retry exceeds fetch budget'))
        return following

    def sleep(self, response=None):
        remaining = _remaining()
        if remaining is None:
            return super().sleep(response)
        delay = _retry_delay(self, response)
        if delay >= remaining:
            raise RequestBudgetExceeded('Fetch HTTP retry exceeds remaining budget')
        if delay:
            time.sleep(delay)
        _remaining()
