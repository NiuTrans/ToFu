"""Guard: the WebSocket transport's correlation-id contract.

Why this exists
---------------
Every ``fetch`` from the Vite transport sends ``X-Request-ID``, and
``server.py``'s ``before_request`` adopts it so all log lines for the request
carry the client's id. The ``/api/push`` WebSocket was the ONE transport left
outside that axis: Quart's ``before_request`` does not run on WS routes, and a
browser ``WebSocket`` cannot set custom headers at all.

Two things therefore have to hold, and both are easy to silently undo:

1. **The id must ride a query param and be honored** — the client mints it in
   ``push.js::_buildUrl`` (``?_rid=…``) and the route resolves it through the
   SAME validated resolver the HTTP path uses (``lib.log.resolve_inbound_rid``),
   so there is one id space and one validation rule across transports.

2. **The WS handler must NOT call ``set_req_id``.** This is the subtle one and
   the reason this file exists. ``lib.log`` stores the rid in a THREAD-LOCAL,
   not a ``ContextVar``. The WS handler is a long-lived coroutine sharing its
   event-loop thread with every HTTP request, so writing the thread-local there
   leaks the socket's id onto unrelated requests. Measured directly: with two
   concurrent HTTP handlers running, the socket coroutine itself ended up
   observing the SECOND handler's id. ``routes/push.py`` therefore passes the
   rid EXPLICITLY to its own log calls.

The ``set_req_id`` assertion is a RATCHET (it inspects the source), which the
charter permits for "block a new violation" guards — but it is anchored on the
AST call graph of the handler function, not on a line number or a source
literal, so a reasonable rewrite of the handler keeps it meaningful.
"""

from __future__ import annotations

import ast
import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
PUSH_PY = os.path.join(ROOT, 'routes', 'push.py')
PUSH_JS = os.path.join(ROOT, 'frontend', 'src', 'runtime', 'app-runtime.js')


def _push_py_src() -> str:
    with open(PUSH_PY, encoding='utf-8') as fh:
        return fh.read()


def _ws_handler_node() -> ast.AsyncFunctionDef:
    """The ``push_ws`` coroutine — located by NAME in the AST, not by line."""
    tree = ast.parse(_push_py_src())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                and node.name == 'push_ws':
            return node
    pytest.fail('routes/push.py no longer defines push_ws — re-point this guard')


def _called_names(node) -> set[str]:
    """Every bare function name called anywhere inside ``node``."""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Name):
                out.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                out.add(fn.attr)
    return out


def test_ws_handler_resolves_the_inbound_rid():
    """The socket must adopt the client's id through the shared resolver."""
    calls = _called_names(_ws_handler_node())
    assert 'resolve_inbound_rid' in calls, (
        'push_ws must resolve the inbound correlation id via '
        'lib.log.resolve_inbound_rid so the WS shares ONE id space (and one '
        'validation rule) with the HTTP path.'
    )


def test_ws_handler_does_not_write_the_threadlocal_rid():
    """set_req_id in this coroutine leaks the socket's id onto HTTP requests.

    lib.log keeps the rid in a thread-local; this handler is long-lived and
    shares its event-loop thread with every request handler.
    """
    calls = _called_names(_ws_handler_node())
    assert 'set_req_id' not in calls, (
        'push_ws must NOT call set_req_id: lib.log stores the rid in a '
        'THREAD-LOCAL, and this coroutine shares its event-loop thread with '
        'every HTTP request — writing it here stamps this socket\'s id onto '
        'unrelated requests (measured: the socket observed a later HTTP '
        "handler's id). Pass the rid explicitly to this socket's log calls."
    )


def test_ws_route_reads_the_rid_query_param():
    """A browser WebSocket cannot set headers — the id rides `_rid`."""
    src = _push_py_src()
    assert "websocket.args.get('_rid')" in src, (
        "routes/push.py must read the `_rid` query param: a browser "
        '`new WebSocket` cannot set a custom X-Request-ID header, so the '
        'query param is the only channel available to it.'
    )


def test_ws_handler_does_not_import_from_server():
    """The resolver lives in lib.log, not behind a route→server import.

    `from server import …` inside a request path is a circular-import hazard
    (nothing else in routes/ or lib/ does it) and would make the WS depend on
    app bootstrap order.
    """
    src = _push_py_src()
    assert 'from server import' not in src, (
        'routes/push.py must not import from server — the shared rid resolver '
        'belongs to lib.log, next to set_req_id/req_id.'
    )


def test_client_puts_the_rid_on_the_socket_url():
    """push.js must actually send an id, else the backend has nothing to adopt."""
    with open(PUSH_JS, encoding='utf-8') as fh:
        js = fh.read()
    assert '_rid=' in js, (
        'the migrated push transport must append `_rid=<id>` to the WS URL; '
        'without it the server mints its own and the client keeps no join key.'
    )
    assert 'encodeURIComponent' in js, (
        'the rid must be URL-encoded when spliced into the socket URL'
    )


def _fn_node(name: str):
    """A module-level function of routes/push.py, located by NAME in the AST."""
    tree = ast.parse(_push_py_src())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                and node.name == name:
            return node
    pytest.fail('routes/push.py no longer defines %s — re-point this guard' % name)


# Socket-event log lines that are REQUIRED to name their socket, keyed by
# a stable substring of the format string. This is a RATCHET list: a line
# that already carries the rid must not lose it. It is deliberately NOT
# "every log call in the frame handlers" — a future diagnostic that has
# nothing to do with socket identity should not be forced to carry an id,
# and demanding that would make the guard a noise source instead of a
# protection. Adding a new socket-event line? Add it here too.
RID_REQUIRED_LINES = (
    'Client abort for task',
    'Client abort for unknown task',
    'Subscribe:',
    'Sender error',
    'Receiver error',
)


def _logger_calls(node):
    """Every ``logger.<level>(...)`` call inside ``node``, any level.

    Yields ``(level, format_text, args_source)``. Level is NOT filtered:
    stamping the rid on a line and later demoting it from info to debug
    must not silently drop it out of the guard's sight — which is exactly
    what an ``attr == 'info'`` filter would do.
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        if not isinstance(fn, ast.Attribute):
            continue
        if not (isinstance(fn.value, ast.Name) and fn.value.id == 'logger'):
            continue
        fmt = sub.args[0] if sub.args else None
        fmt_txt = ''
        for piece in ast.walk(fmt) if fmt is not None else []:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                fmt_txt += piece.value
        yield fn.attr, fmt_txt, ' '.join(ast.dump(a) for a in sub.args[1:])


def _carries_rid(fmt_txt: str, arg_src: str) -> bool:
    """True when the call is genuinely correlated, not just cosmetically.

    Requires BOTH halves: 'rid=' in the format string AND an argument whose
    source mentions a rid. A format string saying `rid=%s` fed a task id
    reads as correlated while being a lie — worse than a bare line, because
    it invites the reader to trust it.
    """
    return 'rid=' in fmt_txt and ('_rid' in arg_src or 'req_id' in arg_src)


def test_frame_handler_logs_carry_the_rid():
    """Every socket-EVENT log line must name the socket it came from.

    This is the whole point of the feature. Stamping only connect/disconnect
    leaves the id useless for the questions people actually ask ("I pressed
    stop and nothing happened") — those are logged by the frame handlers.

    Scanned across push_ws AND both frame handlers, at EVERY log level: the
    covered lines are split across info (abort) and debug (subscribe, the two
    socket-error paths, snapshot-enqueue), so a guard that only looked at
    ``logger.info`` would leave four of the six unprotected — and demoting a
    stamped info line to debug would silently drop it out of sight.
    """
    seen: dict[str, bool] = {}
    for fname in ('push_ws', '_handle_client_frame', '_handle_abort'):
        for _level, fmt_txt, arg_src in _logger_calls(_fn_node(fname)):
            for needle in RID_REQUIRED_LINES:
                if needle in fmt_txt:
                    # A needle can match more than one call (abort has a
                    # known-task and an unknown-task line); ALL must carry it.
                    seen[needle] = seen.get(needle, True) and \
                        _carries_rid(fmt_txt, arg_src)

    missing = sorted(n for n in RID_REQUIRED_LINES if n not in seen)
    assert not missing, (
        'these socket-event log lines vanished from routes/push.py: %r. If a '
        'line was genuinely removed, drop it from RID_REQUIRED_LINES in this '
        'file; if it was renamed, update the needle. Leaving the list stale '
        'turns this guard into a false red, which is how the last one rotted '
        'for 14 days.' % (missing,)
    )

    bare = sorted(n for n, ok in seen.items() if not ok)
    assert not bare, (
        'these socket-event log lines no longer carry a correlation id: %r. '
        'Each must pass the socket\'s rid (rid=%%s fed client.req_id / '
        '_rid / req_id), else a user-supplied rid resolves only to the '
        'connect+disconnect pair and every event in between is unjoinable. '
        'A cosmetic `rid=%%s` fed something that is not a rid does not '
        'count.' % (bare,)
    )


def test_rid_rides_the_push_client():
    """The rid must be carried per-CONNECTION, not as a handler local.

    The frame handlers are module-level functions and cannot see push_ws's
    locals, so PushClient is the carrier (mirroring ``user_id``, which exists
    for exactly this reason). Asserted as behaviour on the real class.
    """
    from lib.agent_core.push import PushClient

    c = PushClient(user_id='u1', req_id='page1-ws3')
    assert c.req_id == 'page1-ws3'
    assert c.user_id == 'u1'
    # Absent id must be the empty string, never None — it is spliced into
    # %s log args and 'None' would read as a real id.
    assert PushClient(user_id=1).req_id == ''
    assert PushClient(user_id=1, req_id=None).req_id == ''


def test_socket_always_has_an_id():
    """There is no 'unidentified socket' state.

    push_ws resolves the rid inside a try/except; BOTH branches must yield a
    usable id. An except branch that fell back to '' would reintroduce a
    socket whose lines join to nothing — the exact hole this closes.
    """
    src = _push_py_src()
    handler = _fn_node('push_ws')
    # Find the try/except that wraps the rid resolution and assert its
    # handler body does not degrade to an empty id.
    seen_try = False
    for sub in ast.walk(handler):
        if not isinstance(sub, ast.Try):
            continue
        body_src = ' '.join(ast.dump(s) for s in sub.body)
        if 'resolve_inbound_rid' not in body_src:
            continue
        seen_try = True
        for h in sub.handlers:
            h_src = ' '.join(ast.dump(s) for s in h.body)
            assert 'resolve_inbound_rid' in h_src, (
                "the rid resolution's except branch must mint an id too "
                '(resolve_inbound_rid(None, None)) — falling back to an '
                'empty rid recreates a socket that cannot be joined to '
                'anything.'
            )
    assert seen_try, 'rid resolution is no longer wrapped — re-point this guard'
    # And the log lines must not paper over a missing id with a placeholder.
    assert "_rid or '-'" not in src, (
        "no `_rid or '-'` placeholder: a socket always has an id now, so the "
        'fallback is dead code that would hide a regression in the resolver.'
    )


def test_rid_validation_rejects_log_injection():
    """The shared resolver must reject anything unsafe for a log line."""
    from lib.log import resolve_inbound_rid, rid_is_safe

    assert resolve_inbound_rid('page1-7') == 'page1-7'
    assert resolve_inbound_rid(None, 'page1-ws2') == 'page1-ws2'
    # Header beats query when both are present and valid.
    assert resolve_inbound_rid('hdr', 'qry') == 'hdr'
    # A CRLF payload could forge whole log records — must be rejected, and
    # rejection means "mint a fresh one", never "sanitize and hand it back".
    forged = 'ok\r\n2026-01-01 [INFO] fake line'
    assert resolve_inbound_rid(forged) != forged
    assert not rid_is_safe(forged)
    assert not rid_is_safe('has space')
    assert not rid_is_safe('x' * 65)
    assert rid_is_safe('x' * 64)
    assert not rid_is_safe(12345)      # non-str input must not crash
    assert not rid_is_safe('')
    assert len(resolve_inbound_rid(None, None)) == 12
