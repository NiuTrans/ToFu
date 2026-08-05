#!/usr/bin/env python3
"""tests/test_yourprovider_gateway_protocol_faces.py — Claude rides the Anthropic wire.

HISTORY OF THIS FILE (read before "simplifying" it)
===================================================
v1 (``test_yourprovider_opus5_gateway_template.py``, deleted 2026-07-28) pinned
"opus-5 must be registered in yourprovider.json" — correct while the gateway's
OpenAI-compatible face was the only route to Claude.

v2 (this file, 2026-07-28) inverted that after the signature migration and
pinned "Claude must NOT be in yourprovider.json; it lives in
yourprovider_claude_code.json" — i.e. it asserted a FILE LAYOUT.

v3 (this file, 2026-07-29) pins the INVARIANT the layout was only ever a
proxy for:

    On a gateway that offers an Anthropic-native face, every Claude-family
    model RESOLVES to that face.

The layout stopped being a valid proxy when ``base_url``/``protocol`` became
per-model (``lib/llm_dispatch/provider_face.py``): one template can now carry
both faces, so "which file is it in" says nothing about which wire it uses.
Asserting through the REAL resolver also covers configurations no template
file describes — a hand-built provider card, or one a template sync mutated.

WHY THE INVARIANT IS A CORRECTNESS FACT, NOT A PREFERENCE
---------------------------------------------------------
Measured live 2026-07-28 on ``yuju-claude-opus-5-evaDaily``: the OpenAI face
streamed 111 chunks / 33 ``reasoning_content`` / **0 signature**; the
Anthropic face delivered ``signature_delta`` intact. A Claude thinking block
replayed WITHOUT its signature is rejected upstream. The drift is silent —
the model appears in the picker and requests succeed; only the reasoning
signature goes missing.

DELIBERATELY HOST-SCOPED — not "Claude ⇒ protocol=anthropic"
------------------------------------------------------------
Bedrock, OpenRouter, shubiaobiao and yeysai all legitimately serve Claude
over OpenAI-compatible endpoints and expose NO Anthropic face; on those hosts
the OpenAI wire is the only option and forbidding it would outlaw working
configs. Classification uses ``lib.model_info._family.is_claude`` (the
backend SSOT that already drives the Claude wire contract) rather than a
hand-rolled regex, so a future ``fable``/``opus`` spelling is covered without
editing this file.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_yourprovider_gateway_protocol_faces.py -v
"""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

# The YourProvider gateway template (yourprovider.json) is internal-only and is NOT
# shipped in opensource exports, so any test that reads it (or audits a
# synthetic payload keyed to its host) can only run in the source tree.
_INTERNAL_TEMPLATE = pytest.mark.skipif(
    is_opensource_build(),
    reason='yourprovider.json is an internal provider template, not shipped in '
           'opensource builds',
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TPL_DIR = os.path.join(_ROOT, 'static', 'provider_templates')

GATEWAY_HOST = 'your-llm-gateway.example.com'
MEITUAN_TEMPLATE = 'yourprovider.json'

# The full Claude roster the gateway serves. The merged template must carry
# all of them — a partial roster silently sends users elsewhere.
EXPECTED_CLAUDE_ROSTER = {
    'claude-opus-5',
    'claude-opus-4.8',
    'claude-opus-4.7',
    'claude-opus-4.6',
    'claude-sonnet-4.6',
    'claude-fable-5',
}

# Gateway-accepted wire ids. The gateway refuses the clean logical names on
# the yuju deployments (HTTP 400 不支持的模型类型), so these must stay in the pool.
REQUIRED_WIRE_IDS = {
    'claude-opus-5': 'yuju-claude-opus-5-evaDaily',
    'claude-opus-4.8': 'aws.claude-opus-4.8',
    'claude-opus-4.7': 'aws.claude-opus-4.7',
    'claude-opus-4.6': 'aws.claude-opus-4.6',
    'claude-sonnet-4.6': 'aws.claude-sonnet-4.6',
}


def _load(fname: str) -> dict:
    with open(os.path.join(_TPL_DIR, fname), encoding='utf-8') as f:
        return json.load(f)


def _all_templates() -> list[tuple[str, dict]]:
    return [(f, _load(f)) for f in sorted(os.listdir(_TPL_DIR))
            if f.endswith('.json')]


def _host(tpl: dict) -> str:
    return (urlparse(tpl.get('base_url') or '').hostname or '').lower()


def _as_provider(tpl: dict) -> dict:
    """A template is a provider card minus credentials — add a key so the
    resolver sees a realistic entry."""
    prov = dict(tpl)
    prov.setdefault('id', tpl.get('key') or 'tpl')
    prov['api_keys'] = ['sk-test']
    return prov


def _audit(templates: list[tuple[str, dict]]) -> list[str]:
    """Return violations of the resolved-wire invariant.

    Drives the REAL resolver, so it audits what would actually be dispatched
    rather than which file an entry happens to live in. Reusable so NEUTER
    faces can push synthetic payloads through the same predicate.
    """
    from lib.llm_dispatch.provider_face import dual_face_hosts, resolve_face
    from lib.model_info._family import is_claude

    known = dual_face_hosts(refresh=True)
    violations: list[str] = []

    for fname, tpl in templates:
        host = _host(tpl)
        prov = _as_provider(tpl)
        for m in (tpl.get('models') or []):
            mid = m.get('model_id') or ''
            if not is_claude(mid):
                continue
            r = resolve_face(prov, m, dual_face_hosts=known)
            if not r.ok:
                violations.append(
                    '%s: Claude model %r is REFUSED (%s)' % (fname, mid, r.error))
                continue
            if host in known and r.protocol != 'anthropic':
                violations.append(
                    '%s: Claude model %r resolves to protocol=%r on host %s, '
                    'which offers an Anthropic-native face — that face is the '
                    'only one emitting thinking-block signatures'
                    % (fname, mid, r.protocol, host))
    return violations


# ═══════════════════════════════════════════════════════════
#  1. The invariant, across every shipped template
# ═══════════════════════════════════════════════════════════

def test_every_claude_model_resolves_to_the_anthropic_wire():
    violations = _audit(_all_templates())
    assert not violations, (
        'resolved-wire violations:\n' + '\n'.join('  ' + v for v in violations))


@_INTERNAL_TEMPLATE
def test_the_yourprovider_gateway_ships_exactly_one_template():
    """The merge's user-visible outcome: one account, one card, one template.

    Two templates for one gateway is the defect this epic removed — it forced
    users to add the same account twice and made the picker show it twice.
    """
    same_host = [f for f, tpl in _all_templates() if _host(tpl) == GATEWAY_HOST]
    assert same_host == [MEITUAN_TEMPLATE], (
        'expected exactly one template for %s, found %r'
        % (GATEWAY_HOST, same_host))


@_INTERNAL_TEMPLATE
def test_the_single_template_declares_both_wire_faces():
    tpl = _load(MEITUAN_TEMPLATE)
    faces = tpl.get('faces') or {}
    assert 'anthropic' in faces, (
        'yourprovider.json must declare faces.anthropic — without it every Claude '
        'entry is refused at slot-build time')
    assert faces['anthropic'].get('protocol') == 'anthropic'
    assert '/v1/anthropic' in (faces['anthropic'].get('base_url') or '')
    assert '/v1/openai/native' in (tpl.get('base_url') or ''), (
        'the default face must stay the OpenAI-compatible one — 37 non-Claude '
        'models depend on it')


@_INTERNAL_TEMPLATE
def test_brand_is_the_gateway_not_a_model_family():
    """The template brands the ACCOUNT. Branding it 'claude' would drop the
    whole card into the Claude group in the picker (core/model_group.js keys
    grouping on brand), splitting one gateway across two sections."""
    assert _load(MEITUAN_TEMPLATE).get('brand') == 'yourprovider'


# ═══════════════════════════════════════════════════════════
#  2. The roster must be complete and dispatchable
# ═══════════════════════════════════════════════════════════

@_INTERNAL_TEMPLATE
def test_template_carries_the_whole_claude_roster():
    have = {(m.get('model_id') or '')
            for m in (_load(MEITUAN_TEMPLATE).get('models') or [])}
    missing = EXPECTED_CLAUDE_ROSTER - have
    assert not missing, 'yourprovider.json is missing %r' % sorted(missing)


@_INTERNAL_TEMPLATE
def test_gateway_accepted_wire_ids_are_preserved():
    """The wire pool is what actually goes out as body['model']."""
    from lib.llm_dispatch.model_entry import resolve_request_ids, routing_group

    models = _load(MEITUAN_TEMPLATE).get('models') or []
    for logical, wire_id in REQUIRED_WIRE_IDS.items():
        entry = next((m for m in models if logical in routing_group(m)), None)
        assert entry is not None, '%s not registered' % logical
        pool = resolve_request_ids(entry)
        assert wire_id in pool, (
            '%s: gateway-accepted id %s missing from pool %r'
            % (logical, wire_id, pool))


@_INTERNAL_TEMPLATE
def test_claude_entries_keep_thinking_capability():
    models = _load(MEITUAN_TEMPLATE).get('models') or []
    by_id = {(m.get('model_id') or ''): m for m in models}
    for logical in EXPECTED_CLAUDE_ROSTER:
        entry = by_id.get(logical)
        assert entry is not None, logical
        caps = set(entry.get('capabilities') or [])
        assert caps == {'text', 'vision', 'thinking'}, (logical, sorted(caps))
        assert entry.get('rpm'), logical
        assert entry.get('cost') is not None, logical


@_INTERNAL_TEMPLATE
def test_the_non_claude_roster_survived_the_merge():
    """The merge must not have cost the OpenAI face any model."""
    have = {(m.get('model_id') or '')
            for m in (_load(MEITUAN_TEMPLATE).get('models') or [])}
    for mid in ('gemini-2.5-flash-image', 'gemini-3-pro-image-preview',
                'text-embedding-3-large', 'kimi-k3', 'gpt-5.6-sol'):
        assert mid in have, '%s missing after the merge' % mid


# ═══════════════════════════════════════════════════════════
#  3. NEUTER faces — prove the predicate discriminates
# ═══════════════════════════════════════════════════════════

@_INTERNAL_TEMPLATE
def test_neuter_audit_flags_a_claude_entry_pinned_to_the_openai_face():
    """The drift shape under the new model: an explicit face pin dragging a
    Claude entry back onto the signature-dropping wire."""
    drifted = [(MEITUAN_TEMPLATE, {
        'base_url': 'https://api.openai.com/v1',
        'faces': {'anthropic': {
            'base_url': 'https://api.openai.com/v1/anthropic',
            'protocol': 'anthropic'}},
        'models': [{'model_id': 'gemini-3.5-flash'},
                   {'model_id': 'claude-opus-5', 'face': 'default'}],
    })]
    violations = _audit(drifted)
    assert any('claude-opus-5' in v for v in violations), violations


@_INTERNAL_TEMPLATE
def test_neuter_audit_flags_a_dropped_anthropic_face():
    """Deleting faces.anthropic while keeping the Claude roster must be
    caught — that is the exact shape a careless template edit produces."""
    drifted = [(MEITUAN_TEMPLATE, {
        'base_url': 'https://api.openai.com/v1',
        'models': [{'model_id': 'claude-opus-5'}],
    })]
    violations = _audit(drifted)
    assert any('claude-opus-5' in v for v in violations), violations


def test_neuter_single_face_hosts_stay_allowed():
    """Bedrock / OpenRouter serve Claude over OpenAI-compat with no Anthropic
    face. If the invariant ever widens to a blanket "Claude ⇒ anthropic",
    this goes red — it would outlaw the only configuration those work with."""
    single_face = [
        ('bedrock.json', {
            'base_url': 'https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1',
            'models': [{'model_id': 'us.anthropic.claude-opus-4-7-v1:0'}]}),
        ('openrouter.json', {
            'base_url': 'https://openrouter.ai/api/v1',
            'models': [{'model_id': 'anthropic/claude-sonnet-4.6'}]}),
    ]
    assert _audit(single_face) == [], (
        'Claude on an OpenAI-compat host with no Anthropic face must stay legal')

    # …and the real shipped set is green (positive control for the above).
    assert _audit(_all_templates()) == []


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
