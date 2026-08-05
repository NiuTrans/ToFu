#!/usr/bin/env python3
"""tests/test_dispatcher_face_wiring.py — the resolver actually governs dispatch.

``lib/llm_dispatch/provider_face.py`` decides which wire face a model uses,
but a pure module decides nothing until the dispatcher consults it. This suite
drives the REAL ``_build_slots_from_providers`` against a MERGED provider card
(one account, two faces) and asserts on the Slots it produces — the objects
that actually carry ``base_url`` / ``protocol`` onto the wire.

WHAT IS PINNED (results, not implementation)
--------------------------------------------
  * A merged card yields Claude slots on the anthropic face and non-Claude
    slots on the default face — one card, two wires.
  * The whole wire pool is preserved per face (request_ids × keys), so the
    merge does not quietly shrink the rotation.
  * A Claude entry on a faces-less card of a dual-face gateway builds NO
    slots and is recorded as a refusal the UI can surface.
  * The refusal is surgical (non-Claude on the same card still builds) and
    host-scoped (single-face gateways keep working).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_dispatcher_face_wiring.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

# The fail-loud refusal is host-scoped to gateways DISCOVERED as dual-face
# from the shipped provider templates. The internal gateway's template is
# not shipped in opensource builds, so its host is legitimately unknown
# there and a faceless card dispatches instead of refusing — by design.
_NEEDS_INTERNAL_DUAL_FACE_HOST = pytest.mark.skipif(
    is_opensource_build(),
    reason='the dual-face host set is derived from the internal gateway '
           'template, which opensource builds do not ship',
)

OPENAI_URL = 'https://api.openai.com/v1'
ANTHROPIC_URL = 'https://api.openai.com/v1/anthropic'

MERGED_CARD = {
    'id': 'default',
    'name': 'Default',
    'base_url': OPENAI_URL,
    'extra_headers': {'X-Custom-Header': 'your-header-value'},
    'api_keys': ['sk-a', 'sk-b'],
    'enabled': True,
    'faces': {
        'anthropic': {'base_url': ANTHROPIC_URL, 'protocol': 'anthropic'},
    },
    'models': [
        {'model_id': 'kimi-k3', 'capabilities': ['text']},
        {'model_id': 'claude-opus-5', 'capabilities': ['text'],
         'request_ids': ['yuju-claude-opus-5-evaDaily']},
        {'model_id': 'claude-opus-4.7', 'capabilities': ['text'],
         'request_ids': ['aws.claude-opus-4.7', 'yuju-claude-opus-4.7-evaDaily']},
    ],
}


def _build(providers):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    d = LLMDispatcher()
    d.slots = []
    d._build_slots_from_providers(providers)
    d._initialized = True
    return d


def _slots_for(d, wire_id):
    return [s for s in d.slots if s.model == wire_id]


# ═══════════════════════════════════════════════════════════
#  1. One card, two wires
# ═══════════════════════════════════════════════════════════

def test_merged_card_dispatches_claude_over_anthropic():
    d = _build([MERGED_CARD])
    got = _slots_for(d, 'yuju-claude-opus-5-evaDaily')
    assert got, 'no slot built for the opus-5 wire id'
    for s in got:
        assert s.protocol == 'anthropic', s
        assert s.base_url == ANTHROPIC_URL, s


def test_merged_card_keeps_non_claude_on_the_default_face():
    d = _build([MERGED_CARD])
    got = _slots_for(d, 'kimi-k3')
    assert got, 'no slot built for kimi-k3'
    for s in got:
        assert s.protocol in ('', 'openai'), s
        assert s.base_url == OPENAI_URL, s


def test_both_wires_coexist_under_one_provider_id():
    """The merge's whole point: ONE provider_id, two protocols."""
    d = _build([MERGED_CARD])
    protos = {s.protocol for s in d.slots}
    assert protos == {'', 'anthropic'} or protos == {'openai', 'anthropic'}, protos
    assert {s.provider_id for s in d.slots} == {MERGED_CARD['id']}


def test_the_whole_wire_pool_survives_the_merge():
    """Each (wire id × key) still becomes its own slot — a merge that silently
    shrank the rotation would degrade throughput invisibly."""
    d = _build([MERGED_CARD])
    for wid in ('aws.claude-opus-4.7', 'yuju-claude-opus-4.7-evaDaily'):
        got = _slots_for(d, wid)
        assert len(got) == 2, (wid, len(got))   # 2 keys
        for s in got:
            assert s.protocol == 'anthropic', s


def test_shared_account_fields_apply_to_both_faces():
    """Keys and headers belong to the ACCOUNT, so both faces inherit them —
    that is what makes one card legitimate."""
    d = _build([MERGED_CARD])
    hdr = 'X-Custom-Header'
    for s in d.slots:
        assert s.extra_headers.get(hdr) == 'your-header-value', s
    assert {s.api_key for s in d.slots} == {'sk-a', 'sk-b'}


# ═══════════════════════════════════════════════════════════
#  2. Fail-loud reaches the dispatcher
# ═══════════════════════════════════════════════════════════

def _faceless_card():
    return {
        'id': 'example_corp_old', 'base_url': OPENAI_URL, 'api_keys': ['sk-a'],
        'enabled': True,
        'models': [
            {'model_id': 'claude-opus-5', 'capabilities': ['text']},
            {'model_id': 'kimi-k3', 'capabilities': ['text']},
        ],
    }


@_NEEDS_INTERNAL_DUAL_FACE_HOST
def test_claude_on_a_faceless_dual_face_card_builds_no_slots():
    """The sync-path defect, at the dispatcher: refuse rather than dispatch
    over the signature-dropping wire."""
    d = _build([_faceless_card()])
    assert not _slots_for(d, 'claude-opus-5'), (
        'a Claude model with no anthropic face on a dual-face gateway must '
        'build NO slots — dispatching it would silently strip signatures')


def test_the_refusal_is_surgical():
    d = _build([_faceless_card()])
    assert _slots_for(d, 'kimi-k3'), 'non-Claude on the same card must survive'


@_NEEDS_INTERNAL_DUAL_FACE_HOST
def test_refusals_are_recorded_for_the_ui():
    """A refusal the user cannot see is its own silent failure. The dispatcher
    must expose what it dropped and why."""
    d = _build([_faceless_card()])
    refusals = getattr(d, 'face_refusals', None)
    assert refusals, 'dispatcher must expose face_refusals'
    joined = ' '.join(r.get('error', '') for r in refusals)
    assert 'claude-opus-5' in joined
    assert 'anthropic' in joined.lower()
    assert any(r.get('provider_id') == 'example_corp_old' for r in refusals)


def test_single_face_gateway_still_serves_claude():
    """Bedrock-shaped config must keep working (host-scoped refusal)."""
    d = _build([{
        'id': 'bedrock',
        'base_url': 'https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1',
        'api_keys': ['sk-a'], 'enabled': True,
        'models': [{'model_id': 'us.anthropic.claude-opus-4-7-v1:0',
                    'capabilities': ['text']}],
    }])
    assert _slots_for(d, 'us.anthropic.claude-opus-4-7-v1:0')


def test_explicit_default_face_pin_dispatches_and_is_not_refused():
    """The documented override: an operator can deliberately keep Claude on
    the default wire."""
    card = _faceless_card()
    card['models'][0]['face'] = 'default'
    d = _build([card])
    got = _slots_for(d, 'claude-opus-5')
    assert got, 'an explicit face pin must dispatch'
    for s in got:
        assert s.base_url == OPENAI_URL, s


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
