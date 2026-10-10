"""First-400-wins at dispatch exhaustion (mtgrjqtuhzi4i9).

Every candidate pair rejected the payload with a DIFFERENT deterministic
HTTP 400. Raising the LAST one lets the last-ditch fallback's 400 mask the
preferred model's actionable rejection (an invalid tool schema). The
exhaust helper re-raises the FIRST 400 (chained from the last for context),
while a non-400 last error still propagates unchanged.
"""

import pytest

from lib.llm_dispatch._api_budget import (
    _raise_dispatch_exhausted,
    _remember_route_missing_error,
)
from lib.llm_dispatch._api_stream_state import _StreamRetryState
from lib.llm_errors import (
    BadRequestError,
    ModelRouteMissingError,
    PermissionError_,
)

pytestmark = pytest.mark.unit


def test_first_bad_request_wins_over_last_fallback_400():
    first = BadRequestError('invalid tool schema: conflicting keywords')
    last = BadRequestError('fallback model also 400ed: context exceeded')
    with pytest.raises(BadRequestError) as exc:
        _raise_dispatch_exhausted(last, max_retries=3, capability='chat',
                                  prefer_model='kimi-k3',
                                  first_err=first, what='dispatch_stream')
    assert exc.value is first
    assert exc.value.__cause__ is last


def test_same_error_unchanged_when_no_distinct_first():
    only = BadRequestError('the one and only 400')
    with pytest.raises(BadRequestError) as exc:
        _raise_dispatch_exhausted(only, max_retries=1, capability='chat',
                                  first_err=only, what='dispatch')
    assert exc.value is only
    assert exc.value.__cause__ is None


def test_last_non_400_still_raised():
    first = BadRequestError('payload rejected')
    last = RuntimeError('gateway 502 on the last slot')
    with pytest.raises(RuntimeError) as exc:
        _raise_dispatch_exhausted(last, max_retries=2, capability='chat',
                                  first_err=first, what='dispatch')
    assert exc.value is last


def test_no_first_err_kwarg_keeps_legacy_behavior():
    last = BadRequestError('only the last error is known')
    with pytest.raises(BadRequestError) as exc:
        _raise_dispatch_exhausted(last, max_retries=2, capability='chat',
                                  what='async_dispatch_stream')
    assert exc.value is last


def test_retry_state_tracks_first_400_field():
    state = _StreamRetryState()
    assert state.first_bad_request_err is None
    first = BadRequestError('first')
    state.first_bad_request_err = state.first_bad_request_err or first
    state.first_bad_request_err = (
        state.first_bad_request_err or BadRequestError('second'))
    assert state.first_bad_request_err is first


def test_route_missing_never_masks_actionable_permission_error():
    permission = PermissionError_('model entitlement denied', status_code=403)
    route_missing = ModelRouteMissingError(
        '不支持的模型类型(model=moonshotai/kimi-k3)',
        'moonshotai/kimi-k3')

    assert _remember_route_missing_error(permission, route_missing) is permission
    assert _remember_route_missing_error(None, route_missing) is route_missing
