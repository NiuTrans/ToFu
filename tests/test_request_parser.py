#!/usr/bin/env python3
"""Unit tests for lib.request_parser."""

import ast
import asyncio
import json
import os
import sys

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install Flask→Quart shim and the production sync-safe wrappers by
# importing server.py the same way production runs do. This patches
# request.get_json() / get_data() / form / files / send_from_directory /
# send_file / make_response to be sync-callable from sync handlers.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            'server.py'))
_mod = _ilu.module_from_spec(_spec)
_mod.__name__ = 'server'
_spec.loader.exec_module(_mod)
_app = _mod.app  # noqa: F841 — keep reference so module-level imports persist


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _make_app():
    from quart import Quart
    return Quart(__name__)


def _expect_bad_request(fn, *args, **kw):
    """Run fn; assert it raised BadRequest. Returns the exception."""
    from lib.request_parser import BadRequest
    try:
        fn(*args, **kw)
    except BadRequest as e:
        return e
    raise AssertionError(f'Expected BadRequest from {fn.__name__}')


_ROUTES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'routes')


def _route_syntax_trees():
    """Yield each retained route once for structural boundary guards."""
    for root, dirs, files in os.walk(_ROUTES_DIR):
        dirs[:] = [name for name in dirs if not name.startswith(('.', '__'))]
        for name in sorted(files):
            if not name.endswith('.py'):
                continue
            path = os.path.join(root, name)
            with open(path, encoding='utf-8') as source_file:
                tree = ast.parse(source_file.read(), filename=path)
            yield os.path.relpath(path, _ROUTES_DIR), tree


# ─── parse_body ──────────────────────────────────────────────────

def _run_in_route(handler_body, payload=None):
    """Run ``handler_body(parse_body)`` inside a real POST route via test_client.

    Returns whatever the handler returns (or raises BadRequest if it does).
    """
    from quart import Quart
    from lib.request_parser import parse_body
    app = Quart(__name__)
    captured = {}

    @app.route('/_t', methods=['POST'])
    def _route():
        try:
            captured['result'] = handler_body(parse_body)
        except Exception as e:
            captured['exc'] = e
        return ('', 204)

    async def _t():
        async with app.test_client() as c:
            kwargs = {}
            if payload is not None:
                kwargs['json'] = payload
            await c.post('/_t', **kwargs)
    asyncio.run(_t())
    if 'exc' in captured:
        raise captured['exc']
    return captured.get('result')


def test_parse_body_dict():
    result = _run_in_route(lambda parse: parse(), payload={'a': 1, 'b': 'hi'})
    assert result == {'a': 1, 'b': 'hi'}
    _ok('parse_body returns the JSON dict')


def test_parse_body_empty():
    result = _run_in_route(lambda parse: parse(), payload=None)
    assert result == {}
    _ok('parse_body() on empty body → {}')


def test_parse_body_non_dict_raises():
    from lib.request_parser import BadRequest
    try:
        _run_in_route(lambda parse: parse(), payload=[1, 2, 3])
    except BadRequest as e:
        assert 'JSON object' in str(e)
        _ok('parse_body() on top-level list → BadRequest')
        return
    raise AssertionError('expected BadRequest')


def test_routes_do_not_bypass_shared_json_parser():
    """Every Quart request-body read stays behind request_parser.

    This is the executable half of docs/API_CONTRACT.md section 3.  An AST
    scan avoids freezing comments/docstrings and recognizes local aliases of
    Quart/Flask's request proxy.
    """
    offenders = []
    for relative_path, tree in _route_syntax_trees():
        request_aliases = {'request'}
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom)
                    and node.module in ('quart', 'flask')):
                for alias in node.names:
                    if alias.name == 'request':
                        request_aliases.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'get_json'):
                continue
            owner = node.func.value
            direct_proxy = (
                isinstance(owner, ast.Name)
                and owner.id in request_aliases
            )
            module_proxy = (
                isinstance(owner, ast.Attribute)
                and owner.attr == 'request'
                and isinstance(owner.value, ast.Name)
                and owner.value.id in ('quart', 'flask')
            )
            if direct_proxy or module_proxy:
                offenders.append(f'{relative_path}:{node.lineno}')
    assert not offenders, (
        'routes must use parse_body()/async_parse_body(), not raw '
        'request.get_json(): ' + ', '.join(sorted(offenders)))


def test_body_validation_is_not_reclassified_as_internal_failure():
    """A broad operational handler must not swallow parser BadRequest.

    The one carve-out is pre-auth browser telemetry: failure to decode an
    already-unauthorized poll must not replace its authoritative 401.  Keeping
    the exception exact and requiring it to remain observed makes the registry
    fail when that diagnostic branch is later removed or relocated.
    """
    allowed = {
        ('browser.py', 'browser_poll'):
            'best-effort locked-out telemetry after auth already failed',
    }
    observed_allowed = set()
    offenders = []

    def _is_parser_call(node):
        if not isinstance(node, ast.Call):
            return False
        return (
            isinstance(node.func, ast.Name)
            and node.func.id in ('parse_body', 'async_parse_body')
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in ('parse_body', 'async_parse_body')
        )

    def _is_broad_handler(handler):
        caught = handler.type
        if caught is None:
            return True
        names = (
            caught.elts if isinstance(caught, ast.Tuple) else (caught,)
        )
        return any(
            isinstance(name, ast.Name)
            and name.id in ('Exception', 'BaseException')
            for name in names
        )

    for relative_path, tree in _route_syntax_trees():
        functions = (
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for function in functions:
            for node in ast.walk(function):
                if not isinstance(node, ast.Try):
                    continue
                body_tree = ast.Module(body=node.body, type_ignores=[])
                parser_calls = [
                    call for call in ast.walk(body_tree)
                    if _is_parser_call(call)
                ]
                if not parser_calls or not any(
                        _is_broad_handler(handler)
                        for handler in node.handlers):
                    continue
                key = (relative_path, function.name)
                if key in allowed:
                    observed_allowed.add(key)
                    continue
                offenders.append(
                    f'{relative_path}:{function.name}:{parser_calls[0].lineno}')

    assert not offenders, (
        'parse_body()/async_parse_body() must run before broad operational '
        'try/except blocks so BadRequest remains a 400: '
        + ', '.join(sorted(offenders)))
    assert observed_allowed == set(allowed), (
        'stale request-parser broad-exception carve-out(s): '
        + ', '.join(
            f'{path}:{function} ({allowed[(path, function)]})'
            for path, function in sorted(set(allowed) - observed_allowed)))


# ─── require_str ──────────────────────────────────────────────────

def test_require_str_present():
    from lib.request_parser import require_str
    assert require_str({'name': 'Alice'}, 'name') == 'Alice'
    _ok('require_str returns string when present')


def test_require_str_strip_default():
    from lib.request_parser import require_str
    assert require_str({'x': '  hello  '}, 'x') == 'hello'
    _ok('require_str strips whitespace by default')


def test_require_str_no_strip():
    from lib.request_parser import require_str
    assert require_str({'x': '  hi  '}, 'x', strip=False) == '  hi  '
    _ok('require_str(strip=False) preserves whitespace')


def test_require_str_missing_raises():
    from lib.request_parser import require_str
    e = _expect_bad_request(require_str, {}, 'name')
    assert e.field == 'name'
    assert 'name is required' in str(e)
    _ok('require_str(missing) → BadRequest with field name')


def test_require_str_empty_raises():
    from lib.request_parser import require_str
    _expect_bad_request(require_str, {'x': ''}, 'x')
    _expect_bad_request(require_str, {'x': '   '}, 'x')  # whitespace-only after strip
    _ok('require_str empty/whitespace-only → BadRequest')


def test_require_str_allow_empty():
    from lib.request_parser import require_str
    assert require_str({'x': ''}, 'x', allow_empty=True) == ''
    _ok('require_str(allow_empty=True) accepts empty string')


def test_require_str_wrong_type_raises():
    from lib.request_parser import require_str
    _expect_bad_request(require_str, {'x': 42}, 'x')
    _expect_bad_request(require_str, {'x': []}, 'x')
    _ok('require_str on non-string → BadRequest')


def test_require_str_max_len():
    from lib.request_parser import require_str
    e = _expect_bad_request(require_str, {'x': 'abcdef'}, 'x', max_len=3)
    assert 'too long' in str(e)
    _ok('require_str(max_len=N) enforces limit')


# ─── optional_str ─────────────────────────────────────────────────

def test_optional_str_default():
    from lib.request_parser import optional_str
    assert optional_str({}, 'x') == ''
    assert optional_str({}, 'x', default='hi') == 'hi'
    assert optional_str({'x': None}, 'x', default='fallback') == 'fallback'
    _ok('optional_str returns default on missing/None')


def test_optional_str_present():
    from lib.request_parser import optional_str
    assert optional_str({'x': 'value'}, 'x') == 'value'
    _ok('optional_str returns value when present')


def test_extractors_accept_read_only_mappings():
    """Field readers depend on Mapping, not mutable route-owned dicts."""
    from types import MappingProxyType
    from lib.request_parser import optional_int, optional_str, require_bool

    body = MappingProxyType({'name': ' Tofu ', 'count': '2', 'ready': True})
    assert optional_str(body, 'name') == 'Tofu'
    assert optional_int(body, 'count') == 2
    assert require_bool(body, 'ready') is True
    _ok('extractors accept read-only Mapping implementations')


# ─── require_int ──────────────────────────────────────────────────

def test_require_int_basic():
    from lib.request_parser import require_int
    assert require_int({'n': 42}, 'n') == 42
    assert require_int({'n': 0}, 'n') == 0
    assert require_int({'n': -10}, 'n') == -10
    _ok('require_int accepts int')


def test_require_int_string_coerced():
    from lib.request_parser import require_int
    assert require_int({'n': '42'}, 'n') == 42
    assert require_int({'n': '  -7  '}, 'n') == -7
    _ok('require_int coerces stringified int')


def test_require_int_float_with_integer_value():
    from lib.request_parser import require_int
    assert require_int({'n': 5.0}, 'n') == 5
    _ok('require_int accepts float with integer value (5.0)')


def test_require_int_rejects_non_integer_float():
    from lib.request_parser import require_int
    _expect_bad_request(require_int, {'n': 5.7}, 'n')
    _ok('require_int rejects non-integer float (5.7)')


def test_require_int_rejects_bool():
    from lib.request_parser import require_int
    # bool is a subclass of int — must be rejected explicitly
    _expect_bad_request(require_int, {'n': True}, 'n')
    _expect_bad_request(require_int, {'n': False}, 'n')
    _ok('require_int rejects bool (despite being int subclass)')


def test_require_int_min_max():
    from lib.request_parser import require_int
    assert require_int({'n': 5}, 'n', min=0, max=10) == 5
    _expect_bad_request(require_int, {'n': -1}, 'n', min=0)
    _expect_bad_request(require_int, {'n': 100}, 'n', max=50)
    _ok('require_int enforces min/max bounds')


def test_require_int_missing_raises():
    from lib.request_parser import require_int
    _expect_bad_request(require_int, {}, 'n')
    _expect_bad_request(require_int, {'n': None}, 'n')
    _ok('require_int(missing) → BadRequest')


def test_optional_int():
    from lib.request_parser import optional_int
    assert optional_int({}, 'n') is None
    assert optional_int({}, 'n', default=10) == 10
    assert optional_int({'n': '42'}, 'n') == 42
    assert optional_int({'n': None}, 'n', default=5) == 5
    _ok('optional_int handles missing/None/value')


# ─── require_bool / optional_bool ─────────────────────────────────

def test_bool_coercion():
    from lib.request_parser import require_bool, optional_bool
    assert require_bool({'x': True}, 'x') is True
    assert require_bool({'x': False}, 'x') is False
    assert require_bool({'x': 'true'}, 'x') is True
    assert require_bool({'x': 'YES'}, 'x') is True
    assert require_bool({'x': 'on'}, 'x') is True
    assert require_bool({'x': '1'}, 'x') is True
    assert require_bool({'x': 'false'}, 'x') is False
    assert require_bool({'x': 'no'}, 'x') is False
    assert require_bool({'x': 0}, 'x') is False
    assert require_bool({'x': 1}, 'x') is True
    _ok('bool coerces from str/int')


def test_bool_invalid_string():
    from lib.request_parser import require_bool
    _expect_bad_request(require_bool, {'x': 'maybe'}, 'x')
    _ok('require_bool rejects garbage strings')


def test_optional_bool_default():
    from lib.request_parser import optional_bool
    assert optional_bool({}, 'x') is False
    assert optional_bool({}, 'x', default=True) is True
    assert optional_bool({'x': None}, 'x', default=True) is True
    _ok('optional_bool returns default on missing')


# ─── require_list / optional_list ─────────────────────────────────

def test_require_list_basic():
    from lib.request_parser import require_list
    assert require_list({'xs': [1, 2, 3]}, 'xs') == [1, 2, 3]
    _ok('require_list returns the list')


def test_require_list_item_type():
    from lib.request_parser import require_list
    assert require_list({'xs': ['a', 'b']}, 'xs', item_type=str) == ['a', 'b']
    e = _expect_bad_request(require_list, {'xs': ['a', 1]}, 'xs', item_type=str)
    assert 'xs[1]' in str(e)
    _ok('require_list(item_type=str) checks every element')


def test_require_list_max_len():
    from lib.request_parser import require_list
    _expect_bad_request(require_list, {'xs': [1, 2, 3]}, 'xs', max_len=2)
    _ok('require_list(max_len=N) enforces limit')


def test_optional_list_default():
    from lib.request_parser import optional_list
    assert optional_list({}, 'xs') == []
    assert optional_list({}, 'xs', default=[1]) == [1]
    assert optional_list({'xs': None}, 'xs') == []
    _ok('optional_list defaults to []')


def test_require_list_wrong_type():
    from lib.request_parser import require_list
    _expect_bad_request(require_list, {'xs': 'not a list'}, 'xs')
    _expect_bad_request(require_list, {'xs': {'a': 1}}, 'xs')
    _ok('require_list rejects non-list')


# ─── require_dict / optional_dict ─────────────────────────────────

def test_require_dict():
    from lib.request_parser import require_dict
    assert require_dict({'cfg': {'a': 1}}, 'cfg') == {'a': 1}
    _expect_bad_request(require_dict, {}, 'cfg')
    _expect_bad_request(require_dict, {'cfg': []}, 'cfg')
    _expect_bad_request(require_dict, {'cfg': 'str'}, 'cfg')
    _ok('require_dict / type checks')


def test_optional_dict():
    from lib.request_parser import optional_dict
    assert optional_dict({}, 'cfg') == {}
    assert optional_dict({}, 'cfg', default={'a': 1}) == {'a': 1}
    assert optional_dict({'cfg': None}, 'cfg') == {}
    _ok('optional_dict default {}')


# ─── BadRequest envelope ─────────────────────────────────────────

def test_bad_request_envelope():
    from lib.request_parser import BadRequest
    e = BadRequest('field x missing', field='x')
    env = e.to_envelope()
    assert env['kind'] == 'bad_request'
    assert env['detail'] == 'field x missing'
    assert env['field'] == 'x'
    _ok('BadRequest.to_envelope() shape')


# ─── Integration with @safe_route + api_response ─────────────────

def test_safe_route_converts_bad_request_to_400():
    """A route raising BadRequest under @safe_route should return 400."""
    from quart import Quart
    from lib.api_response import safe_route, api_ok
    from lib.request_parser import require_str, parse_body

    app = Quart(__name__)

    @app.route('/_t', methods=['POST'])
    @safe_route
    def my_route():
        body = parse_body()
        name = require_str(body, 'name')
        return api_ok({'greet': f'hello {name}'})

    async def _t():
        async with app.test_client() as c:
            # Missing name → 400 with field info
            resp = await c.post('/_t', json={})
            assert resp.status_code == 400
            body = await resp.get_json()
            assert body['ok'] is False
            assert 'name is required' in body['error']
            assert body['field'] == 'name'

            # Valid → 200
            resp = await c.post('/_t', json={'name': 'Bob'})
            assert resp.status_code == 200
            body = await resp.get_json()
            assert body['greet'] == 'hello Bob'

    asyncio.run(_t())
    _ok('@safe_route auto-converts BadRequest → 400 with field')


def test_api_error_with_bad_request_emits_string():
    """api_error(BadRequest) should emit a string error, not an envelope."""
    from quart import Quart
    from lib.api_response import api_error
    from lib.request_parser import BadRequest

    app = Quart(__name__)

    @app.route('/_t')
    def _r():
        e = BadRequest('field x missing', field='x')
        return api_error(e, status=400)

    async def _t():
        async with app.test_client() as c:
            resp = await c.get('/_t')
            assert resp.status_code == 400
            body = await resp.get_json()
            assert body['error'] == 'field x missing'  # string, not dict

    asyncio.run(_t())
    _ok('api_error(BadRequest) emits string (not envelope)')


# ─── decode_proxy_path_arg (VS Code proxy double-encode fix) ─────

def _decode_arg_via_query(qs, name='path'):
    """Invoke decode_proxy_path_arg inside a real request context whose query
    string is ``qs`` (already-URL-encoded, as it would arrive on the wire).
    Quart decodes the query ONCE (mirroring production), then the helper runs.
    """
    from quart import Quart
    from lib.request_parser import decode_proxy_path_arg
    app = Quart(__name__)
    captured = {}

    @app.route('/_t')
    def _r():
        captured['v'] = decode_proxy_path_arg(name)
        return ('', 204)

    async def _t():
        async with app.test_client() as c:
            await c.get('/_t?' + qs)
    asyncio.run(_t())
    return captured['v']


def test_decode_path_plain_and_single_encoded():
    from urllib.parse import quote
    P = '/mnt/your-fs'
    # Single-encoded on the wire → Quart decodes once → clean path, no % left.
    assert _decode_arg_via_query('path=' + quote(P, safe='')) == P
    _ok('decode_proxy_path_arg: single-encoded (direct client) → clean path')


def test_decode_path_double_and_triple_encoded():
    from urllib.parse import quote
    P = '/mnt/your-fs'
    single = quote(P, safe='')
    # Double-encoded (VS Code proxy re-encode): wire=%252F, Quart→%2F, helper→P.
    assert _decode_arg_via_query('path=' + quote(single, safe='')) == P
    # Triple, for good measure (bounded loop still collapses it).
    assert _decode_arg_via_query('path=' + quote(quote(single, safe=''), safe='')) == P
    _ok('decode_proxy_path_arg: double/triple-encoded (proxy) → clean path')


def test_decode_path_empty_and_default():
    assert _decode_arg_via_query('path=') == ''
    # Missing arg → default.
    from quart import Quart
    from lib.request_parser import decode_proxy_path_arg
    app = Quart(__name__)
    cap = {}

    @app.route('/_t')
    def _r():
        cap['v'] = decode_proxy_path_arg('path', default='FALLBACK')
        return ('', 204)

    async def _t():
        async with app.test_client() as c:
            await c.get('/_t')
    asyncio.run(_t())
    assert cap['v'] == 'FALLBACK'
    _ok('decode_proxy_path_arg: empty → "", missing → default')


def test_decode_path_exists_guard_preserves_literal_percent(tmp_path=None):
    """Edge case: a REAL directory whose name legitimately contains a literal
    ``%2f``/``%25`` substring must NOT be over-decoded — the os.path.exists
    short-circuit stops the loop the moment the value is already a real path."""
    import tempfile
    from urllib.parse import quote
    # Create a real dir literally named 'weird%2fname' (a single path segment
    # containing the percent-encoded-looking substring).
    base = tempfile.mkdtemp(prefix='tofu-decode-guard-')
    weird = os.path.join(base, 'weird%2fname')
    os.makedirs(weird, exist_ok=True)
    try:
        # On the wire the client single-encodes it; Quart decodes once →
        # the helper sees the real existing path 'weird%2fname' (contains %2f)
        # and must STOP (exists guard), not decode the %2f into a '/'.
        got = _decode_arg_via_query('path=' + quote(weird, safe=''))
        assert got == weird, f'exists-guard must preserve the literal %2f dir: {got!r}'
        assert os.path.exists(got)
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)
    _ok('decode_proxy_path_arg: exists-guard preserves a real dir named with %2f')


def main():
    print()
    print(_color('═══ request_parser.py Unit Tests ═══', '36'))
    print()
    tests = [
        test_parse_body_dict,
        test_parse_body_empty,
        test_parse_body_non_dict_raises,
        test_routes_do_not_bypass_shared_json_parser,
        test_body_validation_is_not_reclassified_as_internal_failure,
        test_require_str_present,
        test_require_str_strip_default,
        test_require_str_no_strip,
        test_require_str_missing_raises,
        test_require_str_empty_raises,
        test_require_str_allow_empty,
        test_require_str_wrong_type_raises,
        test_require_str_max_len,
        test_optional_str_default,
        test_optional_str_present,
        test_extractors_accept_read_only_mappings,
        test_require_int_basic,
        test_require_int_string_coerced,
        test_require_int_float_with_integer_value,
        test_require_int_rejects_non_integer_float,
        test_require_int_rejects_bool,
        test_require_int_min_max,
        test_require_int_missing_raises,
        test_optional_int,
        test_bool_coercion,
        test_bool_invalid_string,
        test_optional_bool_default,
        test_require_list_basic,
        test_require_list_item_type,
        test_require_list_max_len,
        test_optional_list_default,
        test_require_list_wrong_type,
        test_require_dict,
        test_optional_dict,
        test_bad_request_envelope,
        test_safe_route_converts_bad_request_to_400,
        test_api_error_with_bad_request_emits_string,
        test_decode_path_plain_and_single_encoded,
        test_decode_path_double_and_triple_encoded,
        test_decode_path_empty_and_default,
        test_decode_path_exists_guard_preserves_literal_percent,
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
