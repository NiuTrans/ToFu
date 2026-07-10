"""tests/test_mcp_cred_health.py — MCP credential-health probe.

Covers the "quietly detect an expired Overleaf session cookie" feature:

Transport health (a live subprocess + a good protocol ping) does NOT imply the
stored session cookie/token is still valid — an Overleaf cookie expires (~30
days) while the subprocess stays connected, so ``list_projects`` returns a
*successful* MCP result whose TEXT is an auth-error string. The bridge runs a
read-only ``health_probe`` tool (declared on the catalog entry) and classifies
its output text against ``fail_patterns`` into ok / expired / unknown, then
surfaces it via ``get_cred_health`` for the settings panel.

  * The Overleaf catalog entry ships a ``health_probe`` (list_projects + the
    expired-cookie substrings).
  * ``_run_cred_probe`` classifies an expired-cookie result as 'expired',
    a normal project listing as 'ok', and a raised call as 'unknown'.
  * A server with no ``health_probe`` (custom) is never probed.
  * A successful connect fires the async probe; disconnect drops the state.
  * ``_cred_probe_due`` gates re-probes to once per interval.

Run:  pytest tests/test_mcp_cred_health.py -m unit
"""
from __future__ import annotations

import time

import pytest

import lib.mcp.client as mc
from lib.mcp.client import MCPBridge
from lib.mcp.registry import get_catalog_entry, is_opensource_build
from lib.mcp.types import MCP_CRED_PROBE_INTERVAL

pytestmark = pytest.mark.unit


class _FakeSession:
    pass


def _bridge_with_overleaf(cred_result='ok text'):
    """Bridge with a fake connected 'overleaf' server whose call_tool returns
    ``cred_result`` (or raises if it's an Exception instance)."""
    bridge = MCPBridge()
    handle = mc._MCPServerHandle('overleaf', {})
    handle.session = _FakeSession()
    bridge._servers['overleaf'] = handle
    ns = 'mcp__overleaf__list_projects'
    bridge._tool_index[ns] = {
        'server_name': 'overleaf', 'tool_name': 'list_projects',
        'namespaced_name': ns, 'description': '', 'input_schema': {},
        'openai_def': {}, 'read_only_hint': True,
    }

    def _fake_call_tool(namespaced_name, arguments):
        assert namespaced_name == ns
        if isinstance(cred_result, Exception):
            raise cred_result
        return cred_result

    bridge.call_tool = _fake_call_tool  # type: ignore[method-assign]
    return bridge


# ── Catalog wiring ───────────────────────────────────────

@pytest.mark.skipif(is_opensource_build(),
                    reason='overleaf ships in all builds; guard is defensive')
def test_overleaf_entry_declares_health_probe():
    from lib.mcp.health_probe import validate_health_probe
    entry = get_catalog_entry('overleaf')
    assert entry is not None
    probe = entry.get('health_probe')
    assert isinstance(probe, dict)
    assert probe['tool'] == 'list_projects'
    # The RAW entry pins only the Overleaf-SPECIFIC phrases; generic auth
    # phrases come from DEFAULT_CRED_FAIL_PATTERNS after normalization.
    raw = ' '.join(probe['fail_patterns']).lower()
    assert 'overleaf_session' in raw
    spec = validate_health_probe(probe, server='overleaf')
    assert 'overleaf_session' in spec['fail_patterns']
    assert 'session cookie has expired' in spec['fail_patterns']  # merged default


# ── Classifier ───────────────────────────────────────────

def test_probe_classifies_expired_cookie():
    bridge = _bridge_with_overleaf(
        'Error fetching projects: HTTP 302\n\n'
        'If your session cookie has expired (~30 days), update the '
        'OVERLEAF_SESSION environment variable with a fresh value.')
    rec = bridge._run_cred_probe('overleaf')
    assert rec is not None
    assert rec['status'] == 'expired'
    assert rec['detail']  # a short non-secret snippet is kept for the tooltip
    # And it is surfaced for the UI.
    assert bridge.get_cred_health('overleaf')['status'] == 'expired'


def test_probe_classifies_healthy_listing_as_ok():
    bridge = _bridge_with_overleaf(
        'Your Overleaf projects (3):\n\n  • Thesis  [abc123]\n')
    rec = bridge._run_cred_probe('overleaf')
    assert rec['status'] == 'ok'
    assert rec['detail'] == ''
    assert bridge.get_cred_health('overleaf')['status'] == 'ok'


def test_probe_raise_is_unknown_not_expired():
    """A raised call (transport blip) must classify 'unknown', never 'expired'
    — a transient error must not cry wolf about a still-valid cookie."""
    bridge = _bridge_with_overleaf(RuntimeError('connection reset'))
    rec = bridge._run_cred_probe('overleaf')
    assert rec['status'] == 'unknown'
    assert bridge.get_cred_health('overleaf')['status'] == 'unknown'


def test_probe_noop_without_health_probe_spec():
    """A server with no catalog health_probe (e.g. a custom server) is never
    probed — get_cred_health stays None."""
    bridge = MCPBridge()
    handle = mc._MCPServerHandle('my-custom', {})
    handle.session = _FakeSession()
    bridge._servers['my-custom'] = handle
    assert bridge._cred_probe_spec('my-custom') is None
    assert bridge._run_cred_probe('my-custom') is None
    assert bridge.get_cred_health('my-custom') is None


def test_probe_noop_when_not_connected():
    """No live handle → nothing to probe (avoids call_tool raising 'not
    connected' and mislabelling it)."""
    bridge = MCPBridge()  # overleaf spec resolves, but no server registered
    assert bridge._run_cred_probe('overleaf') is None
    assert bridge.get_cred_health('overleaf') is None


# ── Re-probe gating ──────────────────────────────────────

def test_cred_probe_due_gates_on_interval():
    bridge = _bridge_with_overleaf('ok')
    # Never probed → due immediately (when the periodic probe is enabled).
    if MCP_CRED_PROBE_INTERVAL > 0:
        assert bridge._cred_probe_due('overleaf') is True
    # After a probe, not due again until the interval elapses.
    bridge._run_cred_probe('overleaf')
    assert bridge._cred_probe_due('overleaf') is False
    # Simulate the interval elapsing.
    bridge._cred_probe_ts['overleaf'] = time.time() - (MCP_CRED_PROBE_INTERVAL + 1)
    if MCP_CRED_PROBE_INTERVAL > 0:
        assert bridge._cred_probe_due('overleaf') is True


def test_disconnect_drops_cred_health():
    bridge = _bridge_with_overleaf(
        'Error fetching projects — session cookie has expired')
    bridge._run_cred_probe('overleaf')
    assert bridge.get_cred_health('overleaf') is not None
    bridge._disconnect_one('overleaf', forget=True)
    assert bridge.get_cred_health('overleaf') is None
    assert 'overleaf' not in bridge._cred_probe_ts
