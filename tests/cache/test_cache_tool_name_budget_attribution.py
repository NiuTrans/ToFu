"""A round sent over the route's tool-name budget is a client-side miss, not an upstream one.

Recorded shape (Global Router ``coordinator-989073860b05cc4dbf1a1bad74cc24a9``,
2026-11-08 16:55-17:25, ``kunlunbedrock.claude-opus-5.5-m17`` on
your-llm-gateway.example.com's Anthropic face): every Claude round carried the role's 35
direct tools, 687 joined name bytes against the route's 448-byte budget, and
``[ToolWire] tool-name budget 448 bytes NOT met`` was logged for each one.
All 16 rounds read 0 and re-wrote their whole prompt (79,745 -> 138,624
tokens), yet the per-round records said ``upstream_identical`` /
``cache_write_unsettled``. That verdict sent the investigation to the gateway
account pool (docs/LLM_COST_OPTIMIZATION.md section 7) although the request
itself was uncacheable on this route (section 6). The round knows this at send
time, so its record must name the client-side cause.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit

_ROUTER_SURFACE = {'count': 35, 'name_bytes': 687, 'name_budget_bytes': 448}
_FITTED_SURFACE = {'count': 30, 'name_bytes': 446, 'name_budget_bytes': 448}


def _identical_body():
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, static_prefix_hash, wire_byte_prefix,
    )
    msgs = [{'role': 'system', 'content': 'GLOBAL ROUTER SYSTEM'},
            {'role': 'user', 'content': 'owner message'},
            {'role': 'assistant', 'content': 'routing'}]
    return (msgs, canonical_messages(msgs), static_prefix_hash(msgs),
            wire_byte_prefix(msgs))


def _rounds(monkeypatch, conv, surface):
    """Replay the recorded Router rounds: cold write, then +34 s, then +11 s."""
    import types

    import lib.tasks_pkg.cache_tracking._detect as det
    from lib.tasks_pkg.cache_tracking._state import _cache_states
    from lib.tasks_pkg.wire_fingerprint import routing_fingerprint

    clock = iter((1_000.0, 1_034.0, 1_045.0))
    monkeypatch.setattr(det, 'time', types.SimpleNamespace(time=lambda: next(clock)))
    records = []
    real_info = det.logger.info

    def _capture(fmt, *args):
        if fmt.startswith('[CacheRoundRecord]'):
            records.append(json.loads(args[0]))
        return real_info(fmt, *args)

    monkeypatch.setattr(det.logger, 'info', _capture)
    _cache_states.clear()
    msgs, fp, st, wb = _identical_body()
    routing = routing_fingerprint(
        key_hash='keyAAA', anthropic_beta='',
        endpoint='https://api.openai.com/v1/anthropic/v1/messages',
        affinity='Mt-Session-Id@0123456789ab')
    verdicts = []
    for write in (79_745, 87_331, 90_835):
        usage = {'cache_read_input_tokens': 0, 'cache_creation_input_tokens': write,
                 '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
                 '_wire_routing': dict(routing),
                 '_wire_tool_surface': dict(surface)}
        verdicts.append(det.detect_cache_break(
            conv, msgs, None, 'claude-opus-5.5', usage=usage, user_id=1))
    return verdicts, records


def test_over_budget_round_is_bucketed_client_side(monkeypatch):
    verdicts, records = _rounds(monkeypatch, 'router-over-budget', _ROUTER_SURFACE)

    assert [r['bucket'] for r in records] == [
        'no_break', 'tool_name_budget', 'tool_name_budget']
    for verdict in verdicts[1:]:
        assert 'tool_name_budget' in verdict
        blob = json.dumps(verdict).lower()
        assert 'upstream cache miss' not in blob
        assert '687' in blob and '448' in blob
    assert {r['wire_tool_name_budget_bytes'] for r in records} == {448}


def test_in_budget_round_keeps_the_upstream_verdicts(monkeypatch):
    _, records = _rounds(monkeypatch, 'router-in-budget', _FITTED_SURFACE)

    assert [r['bucket'] for r in records] == [
        'no_break', 'upstream_identical', 'upstream_identical']


def test_classify_verdict_maps_the_budget_key_first():
    from lib.tasks_pkg.cache_tracking._policy import (
        BUCKET_TOOL_NAME_BUDGET, classify_verdict,
    )

    assert classify_verdict({'tool_name_budget': 'x'}) == BUCKET_TOOL_NAME_BUDGET
    assert classify_verdict({'tool_name_budget': 'x', 'cache_write_unsettled': 'y'}) \
        == BUCKET_TOOL_NAME_BUDGET
