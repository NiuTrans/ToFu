#!/usr/bin/env python3
"""tests/test_meituan_claude_code_opus5.py — Claude Opus 5 (yuju evaDaily) registration guard.

Pins the end-to-end registration of Claude Opus 5 on the Meituan Claude Code
(Anthropic-protocol) provider, added 2026-07-25. The ONLY gateway alias
currently available is ``yuju-claude-opus-5-evaDaily`` — same provider and
naming family as the existing ``yuju-claude-opus-4.8-evaDaily`` /
``yuju-claude-opus-4.7-evaDaily`` entries.

Two registration surfaces are audited:

  1. ``static/provider_templates/meituan.json`` — the Settings-UI
     template entry must mirror its 4.8 sibling (caps / rpm / cost). Since
     2026-07-29 this is the MERGED template: one card, two wire faces, with
     the Claude roster routed to ``faces.anthropic`` automatically.
  2. ``lib.model_info._family.is_claude_opus_47`` — the evaDaily alias is
     BARE-MAJOR (``opus-5-…``, no minor digit). The pre-fix regex required a
     ``opus-X.Y`` minor, so the new alias silently classified as Opus ≤4.6:
     no ``thinking.display='summarized'`` (reasoning trace hidden), sampling
     params NOT stripped (Opus 4.7+ contract violation — api.py notes these
     can be HTTP 400), and ``xhigh`` effort downgraded to ``high``.

plus wire-shape parity against ``aws.claude-opus-4.8`` (the reference 4.7+
model) through ``build_body``.

Deliberately NOT pinned: ``DEFAULT_SLOT_CONFIGS`` / ``MODEL_ALIAS_GROUPS``
rows — the evaDaily models carry their caps on the provider template.
``MODEL_PRICING`` rows were added 2026-07-27 with opus-tier pricing
($5/$25 + 1.25/0.10 cache multipliers) and clean ``name`` fields
('Claude Opus 5' etc.) — the display-name channel, not a wire concern.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_meituan_claude_code_opus5.py -v
"""
from __future__ import annotations

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OPUS5 = 'yuju-claude-opus-5-evaDaily'
OPUS48 = 'yuju-claude-opus-4.8-evaDaily'
REF_48 = 'aws.claude-opus-4.8'

_DUMMY_MSGS = [{'role': 'user', 'content': 'hi'}]


def _load_claude_code_template() -> dict:
    """The merged Meituan template.

    Until 2026-07-29 the Claude roster lived in its own
    ``meituan_claude_code.json`` because ``protocol``/``base_url`` were
    provider-level, so one gateway account could not express two wire faces.
    With the account/face separation the roster moved into ``meituan.json``
    (whose ``faces.anthropic`` carries the Anthropic-native wire) and the
    second file was deleted. Only the SOURCE moved — every assertion below is
    about registration and wire shape, which are unchanged.
    """
    path = os.path.join(
        _ROOT, 'static', 'provider_templates', 'meituan.json')
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _template_violations(models: list[dict]) -> list[str]:
    """Audit the template model list: opus-5 registered + field-parity with its
    4.8 sibling.

    Asserts the RESULT — the gateway id is REACHABLE and actually SENT — not
    which config field holds it. Since 2026-07-27 the model-identity contract
    (lib/llm_dispatch/model_entry.py) puts the wire ids in ``request_ids`` and
    leaves ``model_id`` as the clean logical name, so a ``by_id`` lookup would
    go red purely because the id moved fields.

    Reusable so the NEUTER face can feed a synthetic broken list through the
    same predicate."""
    from lib.llm_dispatch.model_entry import resolve_request_ids, routing_group

    violations: list[str] = []
    entry = next((m for m in models if OPUS5 in routing_group(m)), None)
    if entry is None:
        violations.append('%s: missing from template' % OPUS5)
        return violations
    sibling = next((m for m in models if OPUS48 in routing_group(m)), None)
    if sibling is None:
        violations.append('%s: parity sibling missing from template' % OPUS48)
        return violations
    if OPUS5 not in resolve_request_ids(entry):
        violations.append(
            '%s: routable but NEVER SENT (wire pool=%r) — the gateway-accepted '
            'id must be in the request pool'
            % (OPUS5, resolve_request_ids(entry)))
    for field in ('capabilities', 'rpm', 'cost'):
        if entry.get(field) != sibling.get(field):
            violations.append(
                '%s: %s %r != 4.8 sibling %r'
                % (OPUS5, field, entry.get(field), sibling.get(field)))
    if set(entry.get('capabilities') or []) != {'text', 'vision', 'thinking'}:
        violations.append(
            '%s: caps %r != {text, vision, thinking}'
            % (OPUS5, sorted(entry.get('capabilities') or [])))
    return violations


# ═══════════════════════════════════════════════════════════
#  1. Template registration
# ═══════════════════════════════════════════════════════════

@pytest.mark.skipif(is_opensource_build(),
                    reason='meituan.json is an internal provider template, '
                           'not shipped in opensource builds')
def test_template_carries_opus5_with_sibling_parity():
    tpl = _load_claude_code_template()
    violations = _template_violations(tpl.get('models') or [])
    assert not violations, (
        'meituan.json template violations:\n'
        + '\n'.join('  ' + v for v in violations))


# ═══════════════════════════════════════════════════════════
#  2. Family detection — bare-major opus-5 is Opus 4.7+
# ═══════════════════════════════════════════════════════════

def test_opus5_alias_detects_as_claude_opus_47_plus():
    """The whole point of the regex fix: the bare-major evaDaily alias must
    take the Opus 4.7+ wire contract (display=summarized, no sampling params,
    xhigh tier)."""
    from lib.model_info._family import is_claude, is_claude_opus_47
    assert is_claude(OPUS5)
    assert is_claude_opus_47(OPUS5), (
        '%s must classify as Opus 4.7+ — the bare-major form opus-5-<suffix> '
        'carries no minor digit for the legacy opus-X.Y regex' % OPUS5)
    # Future bare / dotted shapes take the same branch.
    assert is_claude_opus_47('claude-opus-5')
    assert is_claude_opus_47('aws.claude-opus-5')
    assert is_claude_opus_47('us.anthropic.claude-opus-5-0-v1:0')


def test_opus47_verdicts_unchanged_for_existing_names():
    """Regression matrix: widening the regex must not flip any pre-existing
    verdict (4.6 line stays False, 4.7/4.8 stay True, non-opus stays False)."""
    from lib.model_info._family import is_claude_opus_47
    expected = {
        'aws.claude-opus-4.8': True,
        'yuju-claude-opus-4.8-evaDaily': True,
        'aws.claude-opus-4.7': True,
        'yuju-claude-opus-4.7-evaDaily': True,
        'claude-opus-4-7': True,
        'us.anthropic.claude-opus-4-7-v1:0': True,
        'aws.claude-opus-4.6': False,
        'vertex.claude-opus-4.6': False,
        'claude-opus-4-6': False,
        'claude-sonnet-4-6': False,
        'aws.claude-sonnet-4.6': False,
        'claude-fable-5': False,          # no 'opus' token
        'yuju-claude-opus-4-evaDaily': False,  # bare FOUR is still < 4.7
    }
    for name, want in expected.items():
        assert is_claude_opus_47(name) is want, (
            '%s: expected %s' % (name, want))


def test_discovery_caps_parity_with_opus_48():
    """Name-based capability inference treats opus-5 exactly like its 4.8
    sibling (explicit template caps are the authoritative source for both)."""
    from lib.llm_dispatch.discovery._capabilities import _infer_capabilities
    assert _infer_capabilities(OPUS5) == _infer_capabilities(OPUS48)


# ═══════════════════════════════════════════════════════════
#  3. Wire shape — build_body parity with the reference 4.7+ model
# ═══════════════════════════════════════════════════════════

def _claude_body(model: str, **kw) -> dict:
    from lib.llm import build_body
    args = dict(max_tokens=4096, thinking_enabled=True, stream=False)
    args.update(kw)
    return build_body(model, copy.deepcopy(_DUMMY_MSGS), **args)


def test_wire_shape_parity_with_reference_opus_48():
    """Same generation contract ⇒ byte-identical bodies modulo the model id:
    thinking adaptive + display=summarized, NO temperature/top_p/top_k."""
    got = _claude_body(OPUS5)
    ref = _claude_body(REF_48)
    assert got.get('thinking') == ref.get('thinking') == {
        'type': 'adaptive', 'display': 'summarized'}
    for key in ('temperature', 'top_p', 'top_k'):
        assert key not in got, '%s must strip %s (Opus 4.7+ contract)' % (OPUS5, key)
        assert key not in ref
    stripped_got = {k: v for k, v in got.items() if k != 'model'}
    stripped_ref = {k: v for k, v in ref.items() if k != 'model'}
    assert stripped_got == stripped_ref


def test_xhigh_effort_not_downgraded_on_opus5():
    """xhigh is the Opus 4.7+ tier — on the un-widened regex opus-5 silently
    downgraded it to high."""
    got = _claude_body(OPUS5, thinking_depth='xhigh')
    assert got.get('effort') == 'xhigh', got.get('effort')
    # …and Tofu's legacy 'ultra' label maps to Claude's top rung (max).
    got_ultra = _claude_body(OPUS5, thinking_depth='ultra')
    assert got_ultra.get('effort') == 'max', got_ultra.get('effort')


# ═══════════════════════════════════════════════════════════
#  4. NEUTER faces — prove the predicates discriminate
# ═══════════════════════════════════════════════════════════

def test_neuter_bare_opus4_is_not_47_plus():
    """If the regex ever degrades to 'any opus ⇒ 4.7+', this face goes red:
    a bare-major FOUR must stay below the 4.7 threshold."""
    from lib.model_info._family import is_claude_opus_47
    assert not is_claude_opus_47('yuju-claude-opus-4-evaDaily')
    assert not is_claude_opus_47('claude-opus-4')


def test_neuter_template_audit_flags_broken_payload():
    """Feed a synthetic broken template through the audit predicate: missing
    opus-5 AND caps-divergent opus-5 must BOTH be flagged. If the audit ever
    degrades to a tautology this face goes red."""
    missing = _template_violations([
        {'model_id': OPUS48, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
    ])
    assert any('missing from template' in v for v in missing), missing
    divergent = _template_violations([
        {'model_id': OPUS48, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
        {'model_id': OPUS5, 'capabilities': ['text'], 'rpm': 30, 'cost': 0.045},
    ])
    assert any(OPUS5 in v and 'caps' in v for v in divergent), divergent
    # …and the audit is green on a payload that is exactly right — in BOTH
    # spellings, so repointing it never outlaws configs already on disk.
    good = [
        {'model_id': OPUS48, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
        {'model_id': OPUS5, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
    ]
    assert _template_violations(good) == []
    good_contract = [
        {'model_id': 'claude-opus-4.8', 'request_ids': [OPUS48],
         'capabilities': ['text', 'vision', 'thinking'], 'rpm': 30, 'cost': 0.045},
        {'model_id': 'claude-opus-5', 'request_ids': [OPUS5],
         'capabilities': ['text', 'vision', 'thinking'], 'rpm': 30, 'cost': 0.045},
    ]
    assert _template_violations(good_contract) == []
    # ★ The half-migration: the entry still ROUTES opus-5 (a per-key cell lists
    # it, so it is in the routing group) but the ENTRY-level wire pool omits it,
    # so the default key dispatches an id the gateway refuses. Silent — nothing
    # raises, the model even appears in the picker — hence a pinned face.
    never_sent = _template_violations([
        {'model_id': 'claude-opus-4.8', 'request_ids': [OPUS48],
         'capabilities': ['text', 'vision', 'thinking'], 'rpm': 30, 'cost': 0.045},
        {'model_id': 'claude-opus-5', 'request_ids': ['claude-opus-5'],
         'key_access': {'0': {'request_ids': [OPUS5]}},
         'capabilities': ['text', 'vision', 'thinking'], 'rpm': 30, 'cost': 0.045},
    ])
    assert any('NEVER SENT' in v for v in never_sent), never_sent
    # A stale `aliases` left behind next to an explicit `request_ids` does NOT
    # smuggle the id back in — the pool is `request_ids` verbatim, so the audit
    # reports the honest verdict (not registered at all) rather than pretending
    # the gateway id is reachable.
    stale_alias = _template_violations([
        {'model_id': 'claude-opus-4.8', 'request_ids': [OPUS48],
         'capabilities': ['text', 'vision', 'thinking'], 'rpm': 30, 'cost': 0.045},
        {'model_id': 'claude-opus-5', 'request_ids': ['claude-opus-5'],
         'aliases': [OPUS5],
         'capabilities': ['text', 'vision', 'thinking'], 'rpm': 30, 'cost': 0.045},
    ])
    assert any('missing from template' in v for v in stale_alias), stale_alias


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
