"""tests/test_mcp_wire_tool_order.py — the tools array that reaches the LLM is
order-stable, pinned at the WIRE PATH rather than at the helper.

WHY THIS EXISTS ALONGSIDE test_cache_schema_stability.py
---------------------------------------------------------
``TestMCPDeterministicOrdering`` already pins that ``get_openai_tool_defs()``
sorts. That is necessary but not sufficient: it drives the helper directly, so
it stays GREEN if someone repoints the actual consumer somewhere else.

There are two orderings in the bridge and only one of them is sorted:

  * ``MCPBridge.get_openai_tool_defs()`` — sorted by namespaced name.
  * ``handle.tools = response.tools`` (``_bridge.py``) — the server's own
    order, stored verbatim per server.

Measured on 2026-07-29, ``lib/tools/registry/_build.py::_build_mcp`` is the
ONLY place that feeds MCP tools into the LLM tool list, and it calls the
sorted helper. ``handle.tools`` reaches only two diagnostic fields
(``tools_count`` / ``tool_names``) and never the wire. So no sorting fix was
needed — but nothing was guarding the ROUTING, which is the part that could
silently regress.

WHAT THIS PINS
--------------
Drive the real ``_build_mcp`` with two bridges holding the same tools in
DIFFERENT server-returned orders (what a reconnect / re-discovery produces)
and assert the emitted array is identical and sorted. A regression that
switched the consumer to the per-server order would make the two runs differ
and turn this red, while the helper-level guards stayed green.

WHY THE NON-EMPTY ASSERTION IS LOAD-BEARING
--------------------------------------------
``_build_mcp`` returns ``[]`` on any failure — disabled via config, bridge not
connected, or any exception (it swallows them into a debug log). An empty list
is trivially "identical" and trivially "sorted", so without an explicit
non-empty check every assertion below passes vacuously. This was observed for
real while writing this test: a bridge missing ``_started`` reported
``connected == False``, ``_build_mcp`` returned ``[]``, and the comparison
"passed" while measuring nothing.
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.unit


def _bridge_with(server_order):
    """A connected MCPBridge whose tool index was populated in ``server_order``.

    Insertion order is what a server's ``tools/list`` response dictates, and it
    is exactly what changes across a reconnect.
    """
    from lib.mcp.client._bridge import MCPBridge
    from lib.mcp.types import make_namespaced_name

    b = MCPBridge.__new__(MCPBridge)
    b._lock = threading.RLock()
    b._tool_index = {}
    b._servers = {'srv': type('H', (), {'session': object(), 'tools': []})()}
    # ``connected`` is a read-only property: ``_started and server_count > 0``.
    # Without this the bridge reads as disconnected and _build_mcp returns [].
    b._started = True

    for name in server_order:
        ns = make_namespaced_name('srv', name)
        b._tool_index[ns] = {
            'server_name': 'srv',
            'tool_name': name,
            'namespaced_name': ns,
            'description': 'd',
            'input_schema': {'type': 'object', 'properties': {}},
            'openai_def': {'type': 'function',
                           'function': {'name': ns, 'description': 'd',
                                        'parameters': {}}},
            'read_only_hint': False,
        }
    return b


def _wire_names(monkeypatch, server_order):
    """Names in the array ``_build_mcp`` hands to the LLM tool list."""
    import lib.mcp as mcpmod
    from lib.tools.registry import _build

    bridge = _bridge_with(server_order)
    assert bridge.connected, (
        'fixture bridge is not connected — _build_mcp would return [] and '
        'every assertion in this module would pass vacuously')

    monkeypatch.setattr(mcpmod, 'get_bridge', lambda: bridge)
    ctx = type('C', (), {'cfg': {'mcpEnabled': True}, 'tid': 't-wire'})()
    out = _build._build_mcp(ctx)

    assert out, ('_build_mcp emitted an EMPTY tools array — the test would be '
                 'vacuous; check the bridge fixture, not the assertion')
    return [d['function']['name'] for d in out]


#: Deliberately not alphabetical, so "sorted" and "server order" differ.
_ORDER_A = ['zebra_tool', 'alpha_tool', 'middle_tool', 'beta_tool']
#: The same tools a server might return after a reconnect, reshuffled.
_ORDER_B = ['beta_tool', 'middle_tool', 'zebra_tool', 'alpha_tool']


def test_wire_array_is_sorted(monkeypatch):
    """The array reaching the LLM is sorted by namespaced name."""
    names = _wire_names(monkeypatch, _ORDER_A)
    assert names == sorted(names), (
        f'tools array reaching the LLM is not sorted: {names}. A shifting '
        f'tools array rewrites the prompt prefix and voids the cache.')


def test_reconnect_reorder_does_not_change_the_wire(monkeypatch):
    """Two different server orders must produce a byte-identical array.

    This is the property the prompt cache depends on: a server that returns
    its tools in a different order after a reconnect must not change our
    request bytes.
    """
    a = _wire_names(monkeypatch, _ORDER_A)
    b = _wire_names(monkeypatch, _ORDER_B)
    assert a == b, (
        f'server reorder changed the wire array:\n  {a}\n  {b}\n'
        f'Every reconnect would then invalidate the whole prompt prefix.')


def test_the_fixture_orders_really_differ():
    """Guard-of-the-guard: the two fixtures must not be the same order.

    If someone "tidied" these lists into the same (or already-sorted) order,
    the reorder test above would compare a sequence with itself and could
    never fail.
    """
    assert _ORDER_A != _ORDER_B
    assert _ORDER_A != sorted(_ORDER_A), (
        '_ORDER_A is already alphabetical — sorted and server order would be '
        'indistinguishable and the sort assertion would be vacuous')


def test_wire_path_consumes_the_sorted_helper(monkeypatch):
    """The wire array must equal the SORTED helper's output, tool for tool.

    Pins the routing, not just the shape: if the consumer were repointed at
    the per-server ``handle.tools`` order, this diverges even though the
    helper-level guards in test_cache_schema_stability.py stay green.
    """
    import lib.mcp as mcpmod
    from lib.tools.registry import _build

    bridge = _bridge_with(_ORDER_A)
    monkeypatch.setattr(mcpmod, 'get_bridge', lambda: bridge)
    ctx = type('C', (), {'cfg': {'mcpEnabled': True}, 'tid': 't-wire'})()

    wire = [d['function']['name'] for d in _build._build_mcp(ctx)]
    helper = [d['function']['name'] for d in bridge.get_openai_tool_defs()]
    assert wire, 'empty wire array — vacuous'
    assert wire == helper, (
        f'wire array does not come from get_openai_tool_defs():\n'
        f'  wire   {wire}\n  helper {helper}')


def test_per_server_handle_order_is_not_what_ships(monkeypatch):
    """Complement: the raw server order must NOT be what reaches the LLM.

    Without this, an implementation that happened to sort in a second place
    would satisfy the tests above while re-introducing a second source of
    truth for ordering.
    """
    names = _wire_names(monkeypatch, _ORDER_A)
    from lib.mcp.types import make_namespaced_name
    raw = [make_namespaced_name('srv', n) for n in _ORDER_A]
    assert names != raw, (
        'the wire array is in the server-returned order — ordering is no '
        'longer normalised on our side')
