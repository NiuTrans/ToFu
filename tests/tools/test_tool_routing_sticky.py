"""Conversation-sticky routed exposure — the turn-boundary cache fix.

WHY
---
Keyword routing (``tools.nativeExposure=routed``, the shipped default) selects
tool families from the LATEST user message. Measured on live kimi-k3 traffic
(2026-09): the resulting tools-array change at ordinary turn boundaries broke
the provider prefix cache at the tools region — every named
turn_boundary_rebill / body_change CacheRoundRecord carried
``<hoisted>.tools`` + ``<bytes>tools``, with full-prefix re-bills at gaps as
short as 62 s. The fix is a UNION-sticky routed set per owner+conversation:
turn-1 routing keeps its cold-start schema saving, later turns only ADD
families, so the wire never shrinks/reshuffles mid-conversation.

These tests drive the REAL ``routed_native_spec_keys`` with a minimal ctx
namespace (no registry, no network).
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

import lib.tools.routing as routing  # noqa: E402


def _ctx(text: str, *, conv_id: str = '', owner: int = 1, **over):
    base = dict(
        messages=[{'role': 'user', 'content': text}],
        conv_id=conv_id,
        owner_user_id=owner,
        search_mode='off',
        search_enabled=False,
        fetch_enabled=False,
        browser_enabled=False,
        desktop_enabled=False,
        image_gen_enabled=False,
        human_guidance_enabled=False,
        scheduler_enabled=False,
        project_enabled=False,
        code_exec_enabled=False,
        cfg={},
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _clean_sticky_state():
    routing._reset_sticky_exposure_for_tests()
    yield
    routing._reset_sticky_exposure_for_tests()


def test_sticky_union_keeps_earlier_families():
    """Turn 2 routes on NEW text but the wire set must NOT retract the family
    turn 1 exposed — the whole-array flip is what broke the prefix cache."""
    conv = 'cv-sticky-union'
    first = routing.routed_native_spec_keys(
        _ctx('搜索最新新闻', conv_id=conv))
    assert {'search', 'fetch', 'search_settings'} <= first
    assert 'browser' not in first

    second = routing.routed_native_spec_keys(
        _ctx('打开浏览器页面点一下', conv_id=conv))
    assert 'browser' in second, f'turn-2 keyword family missing: {second}'
    # The turn-1-only family survives even though turn 2's text never
    # mentions search settings and the fallback only adds search+fetch.
    assert 'search_settings' in second, (
        f'turn-1 family retracted at the turn boundary: {second}')
    assert first <= second, (
        f'routed set shrank across the turn boundary: {first} ⊄ {second}')


def test_sticky_union_is_owner_and_conversation_scoped():
    """Two conversations (or two owners) never share a sticky set — one
    tenant's exposure must not leak into another's wire."""
    a = routing.routed_native_spec_keys(_ctx('搜索最新新闻', conv_id='cv-a'))
    b = routing.routed_native_spec_keys(_ctx('打开浏览器页面', conv_id='cv-b'))
    assert 'search_settings' in a and 'browser' not in a
    assert 'browser' in b and 'search_settings' not in b, (
        f'stickiness leaked across conversations: {b}')
    other_owner = routing.routed_native_spec_keys(
        _ctx('打开浏览器页面', conv_id='cv-a', owner=2))
    assert 'search_settings' not in other_owner, (
        f'stickiness leaked across owners: {other_owner}')


def test_stateless_caller_keeps_request_local_result():
    """Empty conv_id (tests/compat adapters): pure request-local routing and
    NO state recorded — the sticky store stays clean."""
    first = routing.routed_native_spec_keys(_ctx('搜索最新新闻'))
    second = routing.routed_native_spec_keys(_ctx('打开浏览器页面'))
    assert 'search_settings' in first
    assert 'browser' in second and 'search_settings' not in second
    with routing._STICKY_LOCK:
        assert not routing._STICKY_STATE, (
            'stateless assembly must not record sticky state')


def test_sticky_state_survives_unrelated_conversation_churn():
    context = _ctx('搜索', conv_id='cv-retained', owner=1)
    first = routing._union_conversation_sticky(context, {'original-tool'})
    assert first == {'original-tool'}
    for index in range(4097):
        routing._union_conversation_sticky(_ctx('', conv_id=f'cv-{index}', owner=1), {'other-tool'})
    assert routing._union_conversation_sticky(context, {'new-tool'}) == {'original-tool', 'new-tool'}
    assert routing._union_conversation_sticky(_ctx('', conv_id='cv-retained', owner=2), {'foreign-tool'}) == {'foreign-tool'}


def main():
    print()
    print('═══ conversation-sticky routed exposure ═══')
    tests = [
        test_sticky_union_keeps_earlier_families,
        test_sticky_union_is_owner_and_conversation_scoped,
        test_stateless_caller_keeps_request_local_result,
        test_sticky_state_survives_unrelated_conversation_churn,
    ]
    for fn in tests:
        routing._reset_sticky_exposure_for_tests()
        fn()
        print(f'  ✓ {fn.__name__}')
    routing._reset_sticky_exposure_for_tests()
    print(f'═══ ALL {len(tests)} ROUTING-STICKY TESTS PASSED ═══')


if __name__ == '__main__':
    main()
