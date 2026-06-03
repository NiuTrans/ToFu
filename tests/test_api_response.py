#!/usr/bin/env python3
"""Unit tests for lib.api_response.

Validates the response-shape contract that 240+ route migrations depend on.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install Flask→Quart shim before importing routes
import quart as _quart
sys.modules['flask'] = _quart


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


async def _resolve(resp):
    """Extract (status, dict) from a (Response, status) tuple in test ctx.

    Quart's response.get_data() is async, so this helper is too.
    """
    response, status = resp
    if hasattr(response, 'get_data'):
        body = await response.get_data(as_text=True)
    else:
        body = response  # plain string body (api_no_content path)
    return status, (json.loads(body) if body else {})


def _make_app_ctx():
    """Build a minimal app for test_request_context.

    Newer Flask sansio (3.1+) reads ``config['PROVIDE_AUTOMATIC_OPTIONS']``
    in ``add_url_rule``, but the installed Quart dropped it from
    ``default_config`` → bare ``Quart(__name__)`` raises ``KeyError`` on
    construction. ``server.py`` patches ``Quart.default_config`` at import
    time; replicate that here so this module works standalone too (without
    importing the whole server).
    """
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    app = Quart(__name__)
    return app


# ─── Tests ───────────────────────────────────────────────────────────


def test_api_ok_default():
    from lib.api_response import api_ok
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test', method='GET'):
            status, body = await _resolve(api_ok())
            assert status == 200
            assert body == {'ok': True}

    import asyncio
    asyncio.run(_t())
    _ok('api_ok() → 200 {ok: True}')


def test_api_ok_with_data_dict():
    from lib.api_response import api_ok
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_ok({'task_id': 'abc', 'count': 42}))
            assert status == 200
            assert body['ok'] is True
            assert body['task_id'] == 'abc'
            assert body['count'] == 42

    import asyncio
    asyncio.run(_t())
    _ok('api_ok(data) merges dict fields at top level')


def test_api_ok_with_extras():
    from lib.api_response import api_ok
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_ok(taskId='xyz', cursor=10))
            assert status == 200
            assert body == {'ok': True, 'taskId': 'xyz', 'cursor': 10}

    import asyncio
    asyncio.run(_t())
    _ok('api_ok(**extras) injects keyword args at top level')


def test_api_ok_data_and_extras_combined():
    from lib.api_response import api_ok
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_ok({'a': 1}, b=2))
            assert status == 200
            assert body == {'ok': True, 'a': 1, 'b': 2}

    import asyncio
    asyncio.run(_t())
    _ok('api_ok({data}, **extras) merges both')


def test_api_ok_data_non_dict_ignored():
    """api_ok ignores non-dict data parameters (list, int, etc.) silently."""
    from lib.api_response import api_ok
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_ok([1, 2, 3]))  # list ignored
            assert status == 200
            assert body == {'ok': True}

    import asyncio
    asyncio.run(_t())
    _ok('api_ok(non_dict) silently ignored — does not crash')


def test_api_ok_status_is_body_field():
    """api_ok(status='aborting') treats 'status' as a body field, not HTTP code.

    Regression: previously api_ok declared a keyword-only ``status: int``
    HTTP-code parameter which silently swallowed any caller passing
    ``status='aborting' / 'deleted' / 'pending'``. Quart then crashed
    with ``ValueError: invalid literal for int() with base 10: 'aborting'``
    when finalising the response.
    """
    from lib.api_response import api_ok
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_ok(taskId='abc', status='aborting'))
            assert status == 200, f'expected HTTP 200, got {status!r}'
            assert body == {'ok': True, 'taskId': 'abc', 'status': 'aborting'}

    import asyncio
    asyncio.run(_t())
    _ok("api_ok(status='aborting') → 200 with status as body field")


def test_api_created():
    from lib.api_response import api_created
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_created({'id': 'new'}))
            assert status == 201
            assert body == {'ok': True, 'id': 'new'}

    import asyncio
    asyncio.run(_t())
    _ok('api_created() → 201')


def test_api_no_content():
    from lib.api_response import api_no_content
    body, status = api_no_content()
    assert status == 204
    assert body == ''
    _ok('api_no_content() → 204, empty body')


def test_api_error_string():
    from lib.api_response import api_error
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_error('Task not found', status=404))
            assert status == 404
            assert body['ok'] is False
            assert body['error'] == 'Task not found'

    import asyncio
    asyncio.run(_t())
    _ok('api_error("str") preserves string error (legacy shape)')


def test_api_error_envelope():
    from lib.api_response import api_error
    from lib.error_envelope import make_envelope
    app = _make_app_ctx()

    async def _t():
        env = make_envelope('quota', detail='hit limit')
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_error(env, status=429))
            assert status == 429
            assert body['ok'] is False
            assert isinstance(body['error'], dict)
            assert body['error']['kind'] == 'quota'
            assert body['error']['detail'] == 'hit limit'

    import asyncio
    asyncio.run(_t())
    _ok('api_error(envelope) preserves envelope dict')


def test_api_error_exception_classified():
    """api_error(Exception) auto-classifies via from_exception."""
    from lib.api_response import api_error
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            try:
                raise TimeoutError('upstream timed out')
            except TimeoutError as e:
                status, body = await _resolve(api_error(e, status=504,
                                                    context='test', source='unit_test'))
            assert status == 504
            assert body['ok'] is False
            assert isinstance(body['error'], dict)
            # TimeoutError's class name has 'timeout' in it → kind=timeout
            assert body['error']['kind'] == 'timeout'
            assert 'upstream timed out' in body['error']['raw']

    import asyncio
    asyncio.run(_t())
    _ok('api_error(Exception) → typed envelope with classified kind')


def test_api_error_with_extras():
    """Extra fields can be passed alongside the error."""
    from lib.api_response import api_error
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(
                api_error('rate limited', status=429, retry_after=30))
            assert body['ok'] is False
            assert body['error'] == 'rate limited'
            assert body['retry_after'] == 30

    import asyncio
    asyncio.run(_t())
    _ok('api_error("err", retry_after=N) emits extras at top level')


def test_api_bad_request():
    from lib.api_response import api_bad_request
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_bad_request('missing field x'))
            assert status == 400
            assert body == {'ok': False, 'error': 'missing field x'}

    import asyncio
    asyncio.run(_t())
    _ok('api_bad_request → 400')


def test_api_not_found_default():
    from lib.api_response import api_not_found
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_not_found())
            assert status == 404
            assert body['error'] == 'not_found'

    import asyncio
    asyncio.run(_t())
    _ok('api_not_found() → 404 {error: "not_found"}')


def test_api_not_found_custom_message():
    from lib.api_response import api_not_found
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_not_found('Task not found'))
            assert status == 404
            assert body['error'] == 'Task not found'

    import asyncio
    asyncio.run(_t())
    _ok('api_not_found("Task not found") preserves legacy string')


def test_api_unauthorized():
    from lib.api_response import api_unauthorized
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_unauthorized())
            assert status == 401
            assert body['error'] == 'Unauthorized'

    import asyncio
    asyncio.run(_t())
    _ok('api_unauthorized() → 401')


def test_api_forbidden():
    from lib.api_response import api_forbidden
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_forbidden())
            assert status == 403
            assert body['error'] == 'Forbidden'

    import asyncio
    asyncio.run(_t())
    _ok('api_forbidden() → 403')


def test_api_conflict():
    from lib.api_response import api_conflict
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_conflict('name exists'))
            assert status == 409
            assert body['error'] == 'name exists'

    import asyncio
    asyncio.run(_t())
    _ok('api_conflict → 409')


def test_api_payload_too_large():
    from lib.api_response import api_payload_too_large
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_payload_too_large(50 * 1024 * 1024))
            assert status == 413
            assert 'Payload too large' in body['error']
            assert '50.0 MB' in body['error']

    import asyncio
    asyncio.run(_t())
    _ok('api_payload_too_large(N) → 413 with MB hint')


def test_api_method_not_allowed():
    from lib.api_response import api_method_not_allowed
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_method_not_allowed())
            assert status == 405

    import asyncio
    asyncio.run(_t())
    _ok('api_method_not_allowed → 405')


def test_api_internal_error_with_exception():
    """api_internal_error(exc) auto-logs and returns envelope."""
    from lib.api_response import api_internal_error
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            try:
                raise RuntimeError('database exploded')
            except RuntimeError as e:
                status, body = await _resolve(api_internal_error(
                    e, context='test', source='unit', log_traceback=False))
            assert status == 500
            assert body['ok'] is False
            assert isinstance(body['error'], dict)
            assert 'database exploded' in body['error']['raw']

    import asyncio
    asyncio.run(_t())
    _ok('api_internal_error(Exception) → 500 with envelope')


def test_api_internal_error_default():
    from lib.api_response import api_internal_error
    app = _make_app_ctx()

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(api_internal_error())
            assert status == 500
            assert body['ok'] is False
            assert body['error'] == 'internal_error'

    import asyncio
    asyncio.run(_t())
    _ok('api_internal_error() → 500 {error: "internal_error"}')


def test_safe_route_decorator():
    """@safe_route catches uncaught exceptions and returns 500."""
    from lib.api_response import safe_route, api_ok
    app = _make_app_ctx()

    @safe_route
    def crashing_route():
        raise ValueError('bad input')

    @safe_route
    def good_route():
        return api_ok({'value': 1})

    async def _t():
        async with app.test_request_context('/test'):
            # Crashing route → 500 envelope
            status, body = await _resolve(crashing_route())
            assert status == 500
            assert body['ok'] is False
            # Good route → 200 unchanged
            status, body = await _resolve(good_route())
            assert status == 200
            assert body['value'] == 1

    import asyncio
    asyncio.run(_t())
    _ok('@safe_route catches exceptions, lets ok responses pass')



def test_safe_route_decorator_async():
    """@safe_route is dual-mode: an async handler stays awaitable and its
    exceptions are still caught and turned into a 500 envelope."""
    import asyncio

    from lib.api_response import api_ok, safe_route
    app = _make_app_ctx()

    @safe_route
    async def crashing_async():
        raise ValueError('bad input async')

    @safe_route
    async def good_async():
        return api_ok({'value': 2})

    # The decorated async handler MUST remain a coroutine function so Quart
    # awaits it natively instead of serializing a leaked coroutine object.
    assert asyncio.iscoroutinefunction(crashing_async)
    assert asyncio.iscoroutinefunction(good_async)

    async def _t():
        async with app.test_request_context('/test'):
            status, body = await _resolve(await crashing_async())
            assert status == 500
            assert body['ok'] is False
            status, body = await _resolve(await good_async())
            assert status == 200
            assert body['value'] == 2

    asyncio.run(_t())
    _ok('@safe_route (async) stays awaitable; catches async exceptions')


def test_request_id_attached_when_set():
    """error responses include request_id when lib.log.req_id() is set."""
    from lib.api_response import api_internal_error
    from lib.log import set_req_id
    app = _make_app_ctx()

    async def _t():
        set_req_id('test-req-12345')
        try:
            async with app.test_request_context('/test'):
                status, body = await _resolve(api_internal_error('boom'))
                assert body['request_id'] == 'test-req-12345'
        finally:
            set_req_id(None)

    import asyncio
    asyncio.run(_t())
    _ok('error response includes request_id from lib.log')


def test_normalize_error_passthrough_dict_envelope():
    """Existing envelope dicts are passed through unchanged."""
    from lib.api_response import _normalize_error
    from lib.error_envelope import make_envelope
    env = make_envelope('quota', detail='hit limit')
    assert _normalize_error(env) is env  # exact same object
    _ok('_normalize_error(envelope) → passthrough')


def test_normalize_error_arbitrary_dict():
    """Non-envelope dicts are kept as-is (caller knows what they're doing)."""
    from lib.api_response import _normalize_error
    arbitrary = {'custom_field': 'value'}
    assert _normalize_error(arbitrary) is arbitrary
    _ok('_normalize_error(arbitrary_dict) → passthrough')


def test_normalize_error_string_passthrough():
    """Strings are preserved (not auto-wrapped) for legacy compatibility."""
    from lib.api_response import _normalize_error
    assert _normalize_error('Task not found') == 'Task not found'
    _ok('_normalize_error("str") → string preserved')


def test_normalize_error_none():
    from lib.api_response import _normalize_error
    assert _normalize_error(None) is None
    _ok('_normalize_error(None) → None')


def test_normalize_error_exception_to_envelope():
    from lib.api_response import _normalize_error
    try:
        raise ValueError('parse failed')
    except ValueError as e:
        result = _normalize_error(e)
    assert isinstance(result, dict)
    assert 'kind' in result  # envelope shape
    _ok('_normalize_error(Exception) → typed envelope')


def main():
    print()
    print(_color('═══ api_response.py Unit Tests ═══', '36'))
    print()
    tests = [
        test_api_ok_default,
        test_api_ok_with_data_dict,
        test_api_ok_with_extras,
        test_api_ok_data_and_extras_combined,
        test_api_ok_data_non_dict_ignored,
        test_api_ok_status_is_body_field,
        test_api_created,
        test_api_no_content,
        test_api_error_string,
        test_api_error_envelope,
        test_api_error_exception_classified,
        test_api_error_with_extras,
        test_api_bad_request,
        test_api_not_found_default,
        test_api_not_found_custom_message,
        test_api_unauthorized,
        test_api_forbidden,
        test_api_conflict,
        test_api_payload_too_large,
        test_api_method_not_allowed,
        test_api_internal_error_with_exception,
        test_api_internal_error_default,
        test_safe_route_decorator,
        test_safe_route_decorator_async,
        test_request_id_attached_when_set,
        test_normalize_error_passthrough_dict_envelope,
        test_normalize_error_arbitrary_dict,
        test_normalize_error_string_passthrough,
        test_normalize_error_none,
        test_normalize_error_exception_to_envelope,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
