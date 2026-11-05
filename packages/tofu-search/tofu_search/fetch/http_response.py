"""Anonymous HTTP response admission, body limits and resource release.

Entry: read_response_body serves both the ordinary and decoding-recovery paths.
The caller supplies its HttpError type so public exception identity stays with
fetch.http. Request allowances come from request_budget; no new attempt, proxy,
retry policy or provider authority starts here. Failure tracebacks retain error
facts, not this consumer's accumulated transport payload.
"""
from requests.exceptions import Timeout

from tofu_search.fetch.request_budget import RequestBudgetExceeded, remaining_request_budget
from tofu_search.log import get_logger

logger = get_logger(__name__)


def _check_body_budget():
    try:
        remaining_request_budget()
    except RequestBudgetExceeded:
        raise Timeout('Download exceeded body deadline') from None


def read_response_body(response, url, *, max_bytes, http_error):
    """Validate and consume one response; attempt one close on every exit."""
    chunks = []
    chunk = None
    try:
        # Received refusal/status evidence wins over an elapsed body allowance.
        if not response.ok:
            raise http_error(response.status_code, url,
                             retry_after=response.headers.get('Retry-After'))
        length = int(response.headers.get('Content-Length', 0) or 0)
        if length > max_bytes:
            raise http_error(413, url)
        _check_body_budget()
        size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > max_bytes:
                raise http_error(413, url)
            chunks.append(chunk)
            _check_body_budget()
        data = b''.join(chunks)
    except BaseException as original:
        # The propagated exception owns this frame. Discard incomplete transport
        # buffers before close or another attempt, including on interruption.
        chunks.clear()
        chunk = None
        try:
            response.close()
        except BaseException as cleanup_error:
            if isinstance(original, Exception) and not isinstance(cleanup_error, Exception):
                raise  # An interruption takes precedence over an ordinary read failure.
            logger.warning('HTTP response close failed while preserving read failure — %s',
                           url[:80], exc_info=True)
        raise
    chunks.clear()
    chunk = None
    try:
        response.close()
    except BaseException:
        data = None
        raise
    return data
