"""tests/test_mcp_error_classify_rename_safe.py — call-timeout classification
survives an upstream error-class RENAME.

WHY THIS GUARD EXISTS (measured, 2026-07-29)
--------------------------------------------
``lib/mcp/client/_errors.py::_is_call_timeout_error`` used to decide "is this a
tool-call timeout?" with::

    if type(leaf).__name__ in ('TimeoutError', 'McpError'):

That judgement is anchored to a STRING THE UPSTREAM SDK OWNS. mcp 2.x renamed
``McpError`` → ``MCPError`` (acronym consistency), and a name comparison does
not fail loudly on a rename — it simply stops matching, forever, in silence.

What it gates makes the silence expensive. ``_is_call_timeout_error`` drives the
call-level degraded-health circuit (``MCP_DEGRADED_TIMEOUT_STREAK``): after N
consecutive timeouts a server is marked degraded and the next call fast-fails
instead of blocking for the full ``MCP_CALL_TIMEOUT`` again. If classification
silently stops matching, the streak never increments, the gate never trips, and
Tofu goes back to paying a FULL timeout on every call to a stalled server — with
nothing in any log to say the gate died.

This is a defect INDEPENDENT of any SDK upgrade: the same silence would follow
any upstream rename, and it is exactly the "judgement anchored to a string
someone else controls" shape this project has been bitten by before.

The fix resolves the real class and uses ``isinstance``, keeping a name check
covering BOTH spellings as a fallback so classification degrades rather than
disappears when the class cannot be resolved at all.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.mcp.client._errors import (  # noqa: E402
    _is_call_timeout_error,
    _is_transport_dead_error,
    _mcp_error_types,
)

pytestmark = pytest.mark.unit

_TIMEOUT_TEXT = 'Timed out while waiting for response to ClientRequest.'


def _make(name, text, base=Exception):
    """Build an exception instance of a class literally named ``name``."""
    return type(name, (base,), {})(text)


# ── The rename axis: both spellings must classify ────────────────────

@pytest.mark.parametrize('clsname', ['McpError', 'MCPError'])
def test_timeout_classified_under_either_spelling(clsname):
    """A timeout must be recognised whether the SDK calls the class
    ``McpError`` (v1) or ``MCPError`` (v2).

    This is the regression: before the fix, only the v1 spelling matched, so a
    v2 rename silently disabled the degraded-health gate.
    """
    assert _is_call_timeout_error(_make(clsname, _TIMEOUT_TEXT)) is True


def test_isinstance_path_beats_the_name_entirely():
    """A SUBCLASS of the real SDK error classifies even under a name the
    fallback list has never heard of.

    This is what proves the check is anchored to the TYPE rather than to a
    string: ``VendorWrappedTimeout`` appears in no hard-coded list anywhere.
    """
    sdk_types = _mcp_error_types()
    if not sdk_types:
        pytest.skip('SDK error class not resolvable in this env')
    subclass = type('VendorWrappedTimeout', (sdk_types[0],), {})
    exc = subclass.__new__(subclass)
    Exception.__init__(exc, _TIMEOUT_TEXT)
    assert _is_call_timeout_error(exc) is True


def test_resolver_finds_the_real_class_in_this_env():
    """``_mcp_error_types`` must resolve something against the installed SDK.

    Without this, the isinstance path could be dead in every environment and
    the suite would still pass on the name fallback alone — a guard that
    silently tests only half the fix.
    """
    types_found = _mcp_error_types()
    assert types_found, (
        'no SDK protocol-error class resolved; the isinstance path is dead '
        'and classification is running entirely on the name fallback'
    )
    assert all(isinstance(t, type) for t in types_found)
    assert any(t.__name__ in ('McpError', 'MCPError') for t in types_found)


def test_resolver_result_is_cached():
    """Resolution is cached — it runs on every classified exception."""
    assert _mcp_error_types() is _mcp_error_types()


# ── Negative space: the gate must stay conservative ──────────────────

def test_non_timeout_protocol_error_is_not_a_timeout():
    """A genuine tool-level error must NOT trip the timeout gate.

    Over-matching is the opposite failure: it would mark a healthy server
    degraded and fast-fail calls that would have succeeded.
    """
    assert _is_call_timeout_error(_make('MCPError', 'Invalid params: missing "q"')) is False
    assert _is_call_timeout_error(_make('MCPError', 'Tool raised ValueError')) is False


def test_plain_exception_with_timeout_word_is_not_hijacked():
    """An unrelated exception class is not classified merely for containing
    the word 'timeout' — the class identity is half of the judgement."""
    assert _is_call_timeout_error(_make('SomeRandomError', 'timeout')) is False


def test_builtin_timeouts_still_classify():
    """The non-SDK half of the predicate is untouched by the fix."""
    import asyncio
    assert _is_call_timeout_error(TimeoutError('x')) is True
    assert _is_call_timeout_error(asyncio.TimeoutError()) is True


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason='BaseExceptionGroup is builtin only on 3.11+ '
                           '(CI unit matrix includes 3.10)')
def test_exception_group_is_unwrapped_before_classifying():
    """anyio/mcp wrap failures in nested groups; classification must see the
    leaf, not the group."""
    inner = _make('MCPError', _TIMEOUT_TEXT)
    group = BaseExceptionGroup('unhandled errors in a TaskGroup', [inner])  # noqa: F821 — builtin on 3.11+; the skipif above keeps 3.10 from ever reaching this line
    assert _is_call_timeout_error(group) is True


def test_timeout_and_transport_dead_stay_distinct():
    """The two classifiers must not collapse into each other: a timeout is
    retryable-in-place, a dead transport needs a reconnect."""
    timeout = _make('MCPError', _TIMEOUT_TEXT)
    dead = _make('ClosedResourceError', '')
    assert _is_call_timeout_error(timeout) is True
    assert _is_transport_dead_error(dead) is True
    assert _is_call_timeout_error(dead) is False
