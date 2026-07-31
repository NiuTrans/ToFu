#!/usr/bin/env python3
"""tests/test_provider_face_resolution.py — one account, many wire faces.

WHY THIS EXISTS
===============
``protocol`` and ``base_url`` used to exist ONLY at the provider level
(``dispatcher.py`` read them once per provider and copied them onto every
Slot). That made "one gateway, two wire protocols" INEXPRESSIBLE, so the
your-llm-gateway.example.com gateway — one account, one set of API keys, two URL paths —
had to be duplicated into TWO provider cards (``example-corp`` +
``example-corp_anthropic``) and TWO template files. The duplication was never a
design choice; it was the only writable shape.

The split conflated two independent concepts:

  ACCOUNT  — api_keys, extra_headers, billing, quota          (cardinality 1)
  FACE     — base_url + protocol                              (cardinality N)

``lib/llm_dispatch/provider_face.py`` separates them: a provider declares its
alternate wire faces in ``faces{}``, and each model resolves to exactly one.

THE RULE (resolve_face)
=======================
  1. ``model['face']`` names a face explicitly  → use it (escape hatch; also
     the only way to FORCE a Claude model onto the default face).
  2. Claude-family model + provider declares an anthropic face → that face,
     automatically. No per-model config, so a future opus-6 / fable-6 is
     correct the day it is added.
  3. Otherwise → the provider's default base_url / protocol.

FAIL-LOUD, NEVER SILENT DEGRADATION
===================================
Rule 2 must NOT degrade to "use the default face when no anthropic face is
declared". The measured reason: ``_syncFromTemplate`` ADDs every template
entry the provider lacks but NEVER writes provider-level fields (it only
touches model_id / request_ids / capabilities / cost / aliases). So a merged
template carrying 6 Claude models + one click of "sync from template" on an
OLD card that has no ``faces{}`` would land all 6 Claude entries on the
OpenAI face — silently dropping thinking-block signatures, exactly the defect
commit 90202d96 had just fixed, re-entering through a different door.

Therefore: on a gateway host KNOWN to offer an anthropic face, a Claude model
that resolves to a non-anthropic face is REFUSED (``ok=False``) rather than
dispatched. An unavailable model is visible; a silently signature-stripped
one is not.

Deliberately host-scoped — Bedrock / OpenRouter / shubiaobiao / yeysai serve
Claude over OpenAI-compatible endpoints and expose NO anthropic face; on those
hosts the OpenAI wire is the only option and refusing it would outlaw working
configs.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_provider_face_resolution.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

GW = 'your-llm-gateway.example.com'
OPENAI_URL = 'https://api.openai.com/v1'
ANTHROPIC_URL = 'https://api.openai.com/v1/anthropic'

# The merged provider card: ONE account, TWO faces.
MERGED = {
    'id': 'default',
    'name': 'Default',
    'base_url': OPENAI_URL,
    'extra_headers': {'X-Custom-Header': 'your-header-value'},
    'api_keys': ['k1', 'k2', 'k3'],
    'faces': {
        'anthropic': {'base_url': ANTHROPIC_URL, 'protocol': 'anthropic'},
    },
    'models': [
        {'model_id': 'kimi-k3'},
        {'model_id': 'gemini-3.5-flash'},
        {'model_id': 'claude-opus-5',
         'request_ids': ['yuju-claude-opus-5-evaDaily']},
        {'model_id': 'claude-fable-5'},
    ],
}

# The dangerous shape: same gateway host, but NO faces declared (an old card,
# or one a template sync never upgraded).
FACELESS = {
    'id': 'example-corp_old',
    'base_url': OPENAI_URL,
    'api_keys': ['k1'],
    'models': [{'model_id': 'claude-opus-5'}, {'model_id': 'kimi-k3'}],
}

# A single-face host that legitimately serves Claude over OpenAI-compat.
BEDROCK = {
    'id': 'bedrock',
    'base_url': 'https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1',
    'api_keys': ['k1'],
    'models': [{'model_id': 'us.anthropic.claude-opus-4-7-v1:0'}],
}


def _resolve(provider, model, **kw):
    from lib.llm_dispatch.provider_face import resolve_face
    return resolve_face(provider, model, **kw)


# ═══════════════════════════════════════════════════════════
#  1. The core rule — one card, two wires
# ═══════════════════════════════════════════════════════════

def test_claude_auto_selects_the_anthropic_face():
    """Rule 2: no per-model config needed — the family decides."""
    for mid in ('claude-opus-5', 'claude-fable-5'):
        entry = next(m for m in MERGED['models'] if m['model_id'] == mid)
        r = _resolve(MERGED, entry)
        assert r.ok, r.error
        assert r.protocol == 'anthropic', (mid, r.protocol)
        assert r.base_url == ANTHROPIC_URL, (mid, r.base_url)
        assert r.face_name == 'anthropic'


def test_non_claude_models_stay_on_the_default_face():
    """The other 37 models must be untouched by the merge."""
    for mid in ('kimi-k3', 'gemini-3.5-flash'):
        entry = next(m for m in MERGED['models'] if m['model_id'] == mid)
        r = _resolve(MERGED, entry)
        assert r.ok, r.error
        assert r.protocol in ('', 'openai'), (mid, r.protocol)
        assert r.base_url == OPENAI_URL, (mid, r.base_url)


def test_resolution_keys_on_the_wire_pool_not_just_the_logical_name():
    """A logical id that is not itself Claude-spelled but whose wire pool is
    (or vice-versa) must still resolve correctly — the pool is what goes out."""
    entry = {'model_id': 'house-brand-1',
             'request_ids': ['yuju-claude-opus-5-evaDaily']}
    r = _resolve(MERGED, entry)
    assert r.ok, r.error
    assert r.protocol == 'anthropic', (
        'a Claude wire id must select the anthropic face even when the '
        'logical name hides it — the pool is what is dispatched')


def test_explicit_face_pin_wins_over_the_family_rule():
    """Escape hatch: an operator can pin a model to a named face."""
    entry = {'model_id': 'kimi-k3', 'face': 'anthropic'}
    r = _resolve(MERGED, entry)
    assert r.ok, r.error
    assert r.protocol == 'anthropic'
    assert r.base_url == ANTHROPIC_URL


def test_unknown_face_name_is_refused_not_silently_defaulted():
    """A typo'd face must not degrade to the default wire."""
    entry = {'model_id': 'kimi-k3', 'face': 'anthropicc'}
    r = _resolve(MERGED, entry)
    assert not r.ok
    assert 'anthropicc' in (r.error or '')


# ═══════════════════════════════════════════════════════════
#  2. FAIL-LOUD — the sync-path defect this exists to stop
# ═══════════════════════════════════════════════════════════

def test_claude_on_a_faceless_dual_face_gateway_is_refused():
    """THE regression this module exists for.

    An old card on the YourProvider gateway with no ``faces{}`` + a Claude entry
    (which ``_syncFromTemplate`` will happily ADD) must be REFUSED, not
    quietly dispatched over the signature-dropping OpenAI wire.
    """
    entry = {'model_id': 'claude-opus-5'}
    r = _resolve(FACELESS, entry, dual_face_hosts={GW})
    assert not r.ok, (
        'Claude on a faceless card of a KNOWN dual-face gateway must be '
        'refused — silently using the OpenAI face strips thinking signatures')
    assert 'anthropic' in (r.error or '').lower()


def test_non_claude_on_the_same_faceless_card_still_works():
    """The refusal must be surgical: only Claude is affected."""
    r = _resolve(FACELESS, {'model_id': 'kimi-k3'}, dual_face_hosts={GW})
    assert r.ok, r.error
    assert r.base_url == OPENAI_URL


def test_single_face_hosts_keep_serving_claude_over_openai():
    """Bedrock / OpenRouter expose no anthropic face — refusing them would
    outlaw the only configuration those gateways support."""
    entry = BEDROCK['models'][0]
    r = _resolve(BEDROCK, entry, dual_face_hosts={GW})
    assert r.ok, r.error
    assert r.protocol in ('', 'openai')


def test_explicit_pin_to_the_default_face_overrides_the_refusal():
    """An operator who deliberately pins Claude to the default face on a
    dual-face host is making an informed choice — allowed, but the resolver
    reports it so the caller can warn."""
    entry = {'model_id': 'claude-opus-5', 'face': 'default'}
    r = _resolve(MERGED, entry, dual_face_hosts={GW})
    assert r.ok, r.error
    assert r.base_url == OPENAI_URL
    assert r.forced is True, 'an override of the family rule must be flagged'


# ═══════════════════════════════════════════════════════════
#  3. Dual-face host discovery — derived, never hand-copied
# ═══════════════════════════════════════════════════════════

def test_dual_face_hosts_are_derived_from_shipped_templates():
    """The host set must come from data (the shipped templates), not a
    hardcoded list that drifts the moment a template changes."""
    from lib.llm_dispatch.provider_face import dual_face_hosts
    hosts = dual_face_hosts()
    assert GW in hosts, (
        'your-llm-gateway.example.com must be discovered as dual-face from the shipped '
        'templates — got %r' % (sorted(hosts),))
    assert 'bedrock-runtime.us-east-1.amazonaws.com' not in hosts


# ═══════════════════════════════════════════════════════════
#  4. NEUTER faces — prove each guard discriminates
# ═══════════════════════════════════════════════════════════

def test_neuter_family_rule_actually_fires():
    """If the family rule degraded to 'always default face', the auto-select
    tests would be the only thing catching it. Pin it independently: a
    provider WITHOUT an anthropic face on an unknown host falls through to
    the default, proving the rule is conditional rather than constant."""
    plain = {'id': 'x', 'base_url': 'https://example.invalid/v1',
             'api_keys': ['k'], 'models': []}
    r = _resolve(plain, {'model_id': 'claude-opus-5'}, dual_face_hosts=set())
    assert r.ok, r.error
    assert r.base_url == 'https://example.invalid/v1'
    assert r.face_name == 'default'


def test_neuter_refusal_is_not_a_blanket_claude_ban():
    """Complement of the fail-loud test: the SAME Claude model on the SAME
    faceless card is fine when its host is not a known dual-face gateway.
    If the refusal ever widens to 'Claude must always be anthropic', this
    goes red."""
    r = _resolve(FACELESS, {'model_id': 'claude-opus-5'}, dual_face_hosts=set())
    assert r.ok, (
        'the refusal must be host-scoped, not a blanket Claude ban')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
