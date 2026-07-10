"""tests/test_mcp_health_probe_contract.py — the STANDARD credential
health-probe interface (lib/mcp/health_probe.py) + its generalization to any
server.

The Overleaf-specific probe was generalized into a declarative contract any
MCP server (curated catalog OR a user's custom ``mcp_servers.json`` entry) can
opt into. This suite covers the reusable pieces:

  * ``validate_health_probe`` — normalizes a good spec, merges default auth
    patterns, and rejects/repairs malformed specs (feedback for future
    registrations) without raising.
  * ``classify_probe_result`` — the PURE verdict function: structured
    ``login_required`` convention, default auth phrases, explicit per-server
    phrases, EN + 中文, and the critical "don't false-positive on legitimate
    content" property.
  * A CUSTOM server that declares ``health_probe`` in its live config is
    probed with zero catalog entry (the bridge resolves config first).

Run:  pytest tests/test_mcp_health_probe_contract.py -m unit
"""
from __future__ import annotations

import pytest

import lib.mcp.client as mc
from lib.mcp.client import MCPBridge
from lib.mcp.health_probe import (
    DEFAULT_CRED_FAIL_PATTERNS,
    HEALTH_PROBE_SCHEMA,
    classify_probe_result,
    validate_health_probe,
)

pytestmark = pytest.mark.unit


class _FakeSession:
    pass


# ── validate_health_probe ────────────────────────────────

def test_validate_normalizes_and_merges_defaults():
    spec = validate_health_probe(
        {'tool': ' whoami ', 'fail_patterns': ['Custom Boom', 'custom boom']},
        server='x')
    assert spec is not None
    assert spec['tool'] == 'whoami'         # stripped
    assert spec['args'] == {}               # defaulted
    # explicit pattern lowercased + de-duplicated
    assert 'custom boom' in spec['fail_patterns']
    assert spec['fail_patterns'].count('custom boom') == 1
    # defaults merged in, defaults FIRST
    assert spec['fail_patterns'][0] == DEFAULT_CRED_FAIL_PATTERNS[0]
    assert 'not authenticated' in spec['fail_patterns']


def test_validate_rejects_missing_tool():
    assert validate_health_probe({'args': {}}, server='x') is None
    assert validate_health_probe({'tool': '   '}, server='x') is None


def test_validate_none_and_nondict():
    assert validate_health_probe(None) is None
    assert validate_health_probe('nope', server='x') is None
    assert validate_health_probe(123, server='x') is None


def test_validate_repairs_bad_optional_fields():
    # args not a dict → defaulted; fail_patterns not a list → ignored; still
    # returns a usable spec (with defaults) rather than crashing.
    spec = validate_health_probe(
        {'tool': 'ping', 'args': 'oops', 'fail_patterns': 'oops'}, server='x')
    assert spec is not None
    assert spec['args'] == {}
    assert spec['fail_patterns'] == list(DEFAULT_CRED_FAIL_PATTERNS)


# ── classify_probe_result (PURE) ─────────────────────────

def _norm(explicit=None):
    return validate_health_probe(
        {'tool': 't', 'fail_patterns': explicit or []}, server='x')


def test_classify_structured_login_required():
    spec = _norm()
    status, detail = classify_probe_result(
        '{"ok": false, "login_required": true, "error_hint": "run login"}', spec)
    assert status == 'expired'
    assert detail


def test_classify_default_auth_phrase():
    spec = _norm()
    status, _ = classify_probe_result('MCP Error: Not authenticated.', spec)
    assert status == 'expired'


def test_classify_chinese_phrase():
    spec = _norm()
    status, _ = classify_probe_result('错误：登录已过期，请重新登录', spec)
    assert status == 'expired'


def test_classify_explicit_server_phrase():
    spec = _norm(['error fetching projects'])
    status, _ = classify_probe_result(
        'Error fetching projects: HTTP 302 redirect to /login', spec)
    assert status == 'expired'


def test_classify_healthy_is_ok():
    spec = _norm(['error fetching projects'])
    status, detail = classify_probe_result(
        'Your projects (2):\n  • Thesis\n  • Notes', spec)
    assert status == 'ok'
    assert detail == ''


def test_classify_does_not_false_positive_on_legit_content():
    """A legitimate result that merely CONTAINS an auth word as data must not
    flag — but the default patterns are specific multi-word phrases, so a bare
    project title is safe. This documents the intended tradeoff."""
    spec = _norm()
    # A project literally titled with a single auth-ish word.
    status, _ = classify_probe_result(
        'Your projects (1):\n  • login-service-docs [abc]', spec)
    assert status == 'ok'


# ── schema shape (for API advertisement) ─────────────────

def test_schema_advertises_contract():
    assert HEALTH_PROBE_SCHEMA['fields']['tool']['required'] is True
    assert HEALTH_PROBE_SCHEMA['default_fail_patterns']
    assert HEALTH_PROBE_SCHEMA['structured_markers']


# ── custom server: probe declared in LIVE config, no catalog entry ──

def test_custom_server_config_declares_probe():
    """A user's custom server (not in the catalog) opts in by putting
    ``health_probe`` in its mcp_servers.json config. The bridge must resolve it
    from the live handle config and classify accordingly."""
    bridge = MCPBridge()
    cfg = {
        'command': 'my-cli',
        'health_probe': {'tool': 'status', 'fail_patterns': ['boom expired']},
    }
    handle = mc._MCPServerHandle('mycustom', cfg)
    handle.session = _FakeSession()
    bridge._servers['mycustom'] = handle
    ns = 'mcp__mycustom__status'
    bridge._tool_index[ns] = {
        'server_name': 'mycustom', 'tool_name': 'status',
        'namespaced_name': ns, 'description': '', 'input_schema': {},
        'openai_def': {}, 'read_only_hint': True,
    }
    # Spec resolves from config (no catalog entry for 'mycustom').
    spec = bridge._cred_probe_spec('mycustom')
    assert spec is not None
    assert spec['tool'] == 'status'

    bridge.call_tool = lambda n, a: 'service reports: BOOM EXPIRED token'  # type: ignore[method-assign]
    rec = bridge._run_cred_probe('mycustom')
    assert rec['status'] == 'expired'
    assert bridge.get_cred_health('mycustom')['status'] == 'expired'


def test_custom_server_without_probe_is_noop():
    bridge = MCPBridge()
    handle = mc._MCPServerHandle('plain', {'command': 'x'})
    handle.session = _FakeSession()
    bridge._servers['plain'] = handle
    assert bridge._cred_probe_spec('plain') is None
    assert bridge._run_cred_probe('plain') is None


# ── Dual-defense: an empty pattern list still falls back to defaults ──

def test_empty_patterns_still_catches_via_default_fallback():
    """Even a raw spec with an empty ``fail_patterns`` list must not blind the
    classifier — it falls back to DEFAULT_CRED_FAIL_PATTERNS. This locks the
    belt-and-suspenders default so a mis-normalized spec can't silently miss an
    obvious auth phrase."""
    empty_spec = {'tool': 't', 'args': {}, 'fail_patterns': []}
    status, _ = classify_probe_result('MCP Error: not authenticated', empty_spec)
    assert status == 'expired'


# ── NEUTER (in-memory, read-only): structured markers are load-bearing ──

def test_NEUTER_without_structured_markers_misses_json_login_required(monkeypatch):
    """A structured ``{"login_required": true}`` result uses the UNDERSCORE
    form, which no free-text phrase (all use a space, e.g. "login required")
    matches. So STRUCTURED_EXPIRED_MARKERS is strictly required to catch it —
    neuter the markers to empty and the JSON is MISSED, proving they carry the
    detection for the project's structured auth convention."""
    import lib.mcp.health_probe as hp
    spec = validate_health_probe({'tool': 't'}, server='x')  # defaults merged
    json_result = '{"ok": false, "login_required": true}'
    # Real behavior: caught.
    status, _ = classify_probe_result(json_result, spec)
    assert status == 'expired'
    # Neutered markers → missed (bites).
    monkeypatch.setattr(hp, 'STRUCTURED_EXPIRED_MARKERS', ())
    status2, _ = classify_probe_result(json_result, spec)
    assert status2 == 'ok'
