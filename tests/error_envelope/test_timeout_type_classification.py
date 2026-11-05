"""A deadline stop is a retryable ``timeout`` whatever its name or message says.

``TranslationDeadlineExhausted`` is a ``TimeoutError`` whose name contains
neither "timeout" nor "timed out", so the name/message heuristics classified a
paper or PPTX translation deadline stop as ``generic`` (or as ``ratelimit``
when its ``last_err`` quoted an HTTP 429).
"""
import pytest

from lib.error_envelope import from_exception
from lib.translate.errors import TranslationDeadlineExhausted

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('last_err', [
    None,
    'stopped at the caller deadline: streaming translation candidate: '
    'provider stream is not verified complete (state=client_aborted)',
    'AbortedError: User aborted while awaiting response headers',
    'dispatch error: API HTTP 429: too many requests',
])
def test_translation_deadline_stop_is_a_retryable_timeout(last_err):
    stop = TranslationDeadlineExhausted(
        deadline_seconds=30, dispatched_attempts=1, elapsed_seconds=30.2,
        last_err=last_err)

    envelope = from_exception(stop, context='paper-translate',
                              source='routes.paper:translate')

    assert envelope['kind'] == 'timeout'
    assert envelope['retryable'] is True


def test_undispatched_deadline_stop_is_a_retryable_timeout():
    stop = TranslationDeadlineExhausted(deadline_seconds=180,
                                        dispatched_attempts=0)

    envelope = from_exception(stop, context='pptx-translate',
                              source='routes.translate:pptx')

    assert envelope['kind'] == 'timeout'
    assert envelope['retryable'] is True
