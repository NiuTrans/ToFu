"""Route error-taxonomy guard.

Only typed client faults may be answered with a 4xx. A builtin exception is
not evidence that the caller made a mistake: Python raises ``ValueError``,
``TypeError``, ``KeyError`` and ``LookupError`` for programming and
durable-data faults too. Those must reach the redacted 500 boundary.

This is an AST guard over every route module, not a count of legacy debt.
Handlers that catch a builtin around an application/service call and return
``api_bad_request`` / ``api_not_found`` / ``api_conflict`` /
``api_forbidden`` / ``api_unauthorized`` are rejected. Catching a builtin to
log, re-raise, or convert it into a typed domain error is fine. A handler
whose protected block only parses HTTP input (numeric conversion, field
validators) is still a client 400; the guard distinguishes that block from
application work instead of banning useful input validation. Compatibility endpoints with a deliberately locked non-JSON status
are outside this rule and must be listed explicitly with their reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
BUILTIN_FAULTS = frozenset({
    'ArithmeticError', 'AttributeError', 'BaseException', 'Exception',
    'IndexError', 'KeyError', 'LookupError', 'OverflowError', 'RuntimeError',
    'TypeError', 'ValueError',
})
CLIENT_RESPONSES = frozenset({
    'api_bad_request', 'api_conflict', 'api_not_found',
    'api_forbidden', 'api_unauthorized',
})
# Deliberate protocol/status carve-outs. Do not add a route merely because
# converting its service/domain errors is inconvenient; fix the domain type.
CARVE_OUTS: dict[str, str] = {}


def _caught_names(handler: ast.ExceptHandler) -> set[str]:
    node = handler.type
    if node is None:
        return {'BaseException'}
    nodes = node.elts if isinstance(node, ast.Tuple) else [node]
    return {item.id for item in nodes if isinstance(item, ast.Name)}


def _client_response_calls(nodes) -> set[str]:
    names = set()
    for statement in nodes:
        for node in ast.walk(statement):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in CLIENT_RESPONSES:
                    names.add(node.func.id)
    return names


def _route_files():
    for path in sorted((ROOT / 'routes').rglob('*.py')):
        yield path


def _protected_application_work(handler: ast.ExceptHandler) -> bool:
    """Whether the try body does more than decode request data.

    The AST parent of an ExceptHandler is its Try. Calls to ``int``/``float``
    and other local scalar conversions are request parsing. Any other call is
    treated as application work and must not use a builtin exception as its
    client-fault classifier.
    """
    parent = getattr(handler, '_try_parent', None)
    if parent is None:
        return True
    for statement in parent.body:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {'int', 'float', 'str', 'bool', 'len', 'isinstance'}:
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {'min', 'max', 'abs', 'round', 'sorted', 'set', 'list', 'dict', 'tuple'}:
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr in {'strip', 'lower', 'upper', 'split', 'isdecimal', 'isdigit', 'isascii', 'encode', 'decode', 'get', 'getlist'}:
                continue
            return True
    return False


def test_routes_do_not_answer_builtin_exceptions_as_client_faults():
    offenders = []
    for path in _route_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in CARVE_OUTS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for parent in ast.walk(tree):
            if not isinstance(parent, ast.Try):
                continue
            for handler in parent.handlers:
                handler._try_parent = parent
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            if not _caught_names(handler) & BUILTIN_FAULTS:
                continue
            if not _protected_application_work(handler):
                continue
            responses = _client_response_calls(handler.body)
            if responses:
                offenders.append(
                    f'{rel}:{handler.lineno} catches '
                    f'{sorted(_caught_names(handler) & BUILTIN_FAULTS)} '
                    f'and answers {sorted(responses)}'
                )
    assert not offenders, (
        'Builtin exceptions are server faults, not client faults. Raise '
        'BadRequest only for caller-caused input, and let other exceptions '
        'reach the redacted 500 boundary:\n' + '\n'.join(offenders)
    )


def test_carve_outs_are_real_files():
    missing = [rel for rel in CARVE_OUTS if not (ROOT / rel).is_file()]
    assert not missing, f'Stale route error-taxonomy carve-outs: {missing}'
