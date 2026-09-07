#!/usr/bin/env python3
"""tests/test_claude_opus5_thinking_disabled.py — "thinking off" must be SAID, not implied.

Regression class pinned
=======================
Claude Opus 5 (2026-07-24) runs **adaptive thinking ON BY DEFAULT**: a request
that carries NO ``thinking`` field thinks anyway. Every Claude generation
before it defaulted OFF, so Tofu's "thinking disabled" path expressed the
intent by OMITTING the key — correct for 4.6/4.7/4.8, silently wrong for 5.

Consequence on the user-visible knob (index.html ``data-depth="off"``): the
user asks for thinking OFF, Tofu sends nothing, and Opus 5 thinks at full
depth. The user pays for reasoning tokens they explicitly declined.

LIVE gateway evidence (yuju-claude-opus-5-evaDaily via your-llm-gateway.example.com,
2026-07-26, same prompt, max_tokens=8000, 4 samples each):

    body shape                      completion_tokens          median
    ------------------------------  -------------------------  ------
    OMIT thinking  (what we sent)   [2271, 3916, 1487, 3580]    2925.5
    thinking={"type":"disabled"}    [2043, 1003, 1369, 1667]    1518.0
                                                        ratio ≈ 1.93x

Same run also measured latency on the identical prompt: 36.3s/24.8s (omit)
vs 19.4s/19.6s (explicit disabled).

Scope of the fix — why gated on ``is_claude_opus_47`` and not all Claude
-----------------------------------------------------------------------
``thinking: {"type": "disabled"}`` is the DOCUMENTED disable form for the
adaptive-thinking generation (Opus 4.7+), so it is safe on every model that
predicate matches. It is NOT sent to pre-4.7 Claude, whose older API revisions
were never verified to accept the key — those keep the byte-identical
omit-the-key behaviour they have today.

Verified live that the fix is a NO-OP where thinking already defaulted off
(aws.claude-opus-4.7, 3 samples): omit=[562, 536, 628] vs
disabled=[462, 535, 601] — statistically identical, no error. So the branch
cannot regress 4.7/4.8; it only corrects 5+.

Deliberately NOT asserted: any effort/token number from the live gateway.
These tests are hermetic — they pin the BODY SHAPE our builders emit. The
live numbers above are the evidence that shape matters, not a test oracle.

★ The ``effort`` co-assertion is not incidental. Anthropic returns HTTP 400
for ``thinking:{"type":"disabled"}`` combined with effort ``xhigh``/``max``
(Opus 5 release notes). Both builders must therefore emit the disable form
WITHOUT an effort key, or "thinking off at max depth" becomes a hard 400 on
a path that previously merely wasted tokens. test_disabled_never_ships_effort
is what keeps a future "always forward effort" refactor from shipping that.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_claude_opus5_thinking_disabled.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

# The production id (server_config.json presets.opus) and the clean API id.
# Both must behave identically — is_claude_opus_47's bare-major regex parses
# the gateway-prefixed evaDaily id as (5, 0).
OPUS5_IDS = ('yuju-claude-opus-5-evaDaily', 'claude-opus-5')
# Adaptive-thinking generation that already defaults thinking OFF. The fix
# must be a no-op in OUTCOME here (still no thinking), which we express by
# asserting the disable form is present-and-harmless rather than absent.
OPUS47_IDS = ('aws.claude-opus-4.7', 'aws.claude-opus-4.8')
# Pre-adaptive Claude — must keep the historical omit-the-key wire byte-for-byte.
LEGACY_CLAUDE_IDS = ('claude-3-5-sonnet-20241022', 'aws.claude-opus-4.6')

_MSGS = [{'role': 'user', 'content': 'hi'}]


def _build(model, **kw):
    from lib.llm.body import build_body
    return build_body(model, [dict(m) for m in _MSGS], **kw)


# ══════════════════════════════════════════════════════════
#  build_body — the primary request path
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize('model', OPUS5_IDS)
@pytest.mark.parametrize('kw', [
    {'thinking_depth': 'off'},        # the UI knob: data-depth="off"
    {'thinking_enabled': False},      # programmatic callers
], ids=['depth_off', 'thinking_enabled_false'])
def test_opus5_thinking_off_sends_explicit_disabled(model, kw):
    """★ THE BUG. Opus 5 thinks unless told not to — so we must tell it."""
    body = _build(model, **kw)
    assert body.get('thinking') == {'type': 'disabled'}, (
        'model=%s kw=%s emitted thinking=%r. Opus 5 defaults adaptive thinking '
        'ON, so omitting the key does NOT disable it — the user who picked '
        '"off" still pays ~1.93x completion tokens (live-measured). Send an '
        'explicit {"type": "disabled"}.' % (model, kw, body.get('thinking')))


@pytest.mark.parametrize('model', OPUS5_IDS)
@pytest.mark.parametrize('depth', ['off', 'low', 'medium', 'high', 'xhigh', 'max'])
def test_disabled_never_ships_effort(model, depth):
    """thinking=disabled + effort xhigh/max is HTTP 400 (Opus 5 release notes).

    Covers the whole ladder, not just the dangerous rungs: the invariant is
    "the disable form never carries effort", which is what makes the two
    documented-illegal combinations unreachable by construction.
    """
    body = _build(model, thinking_enabled=False, thinking_depth=depth)
    if body.get('thinking') == {'type': 'disabled'}:
        assert 'effort' not in body, (
            'model=%s depth=%s shipped thinking=disabled WITH effort=%r — '
            'Anthropic returns HTTP 400 for disabled+xhigh/max.'
            % (model, depth, body.get('effort')))


@pytest.mark.parametrize('model', OPUS5_IDS + OPUS47_IDS)
def test_thinking_on_path_is_unchanged(model):
    """The fix must not disturb the (already correct) thinking-ON branch."""
    body = _build(model, thinking_enabled=True, thinking_depth='xhigh')
    assert body.get('thinking') == {'type': 'adaptive', 'display': 'summarized'}
    assert body.get('effort') == 'xhigh'
    # Opus 4.7+ rejects sampling params.
    for k in ('temperature', 'top_p', 'top_k'):
        assert k not in body, 'sampling param %s leaked for %s' % (k, model)


@pytest.mark.parametrize('model', LEGACY_CLAUDE_IDS)
def test_pre_adaptive_claude_wire_unchanged(model):
    """Pre-4.7 Claude keeps the historical omit-the-key disable wire.

    Those API revisions were never verified to accept thinking={"type":
    "disabled"}, and they already default thinking OFF, so there is nothing
    to fix and a speculative key would be an unforced 400 risk.
    """
    body = _build(model, thinking_enabled=False)
    assert 'thinking' not in body, (
        'model=%s gained a thinking key on the disable path. Pre-adaptive '
        'Claude must keep the byte-identical omit wire.' % model)


# ══════════════════════════════════════════════════════════
#  _readjust_thinking_params — the model-SWAP path
# ══════════════════════════════════════════════════════════
#
# Second site with the same defect. A body built for another family and then
# swapped onto Opus 5 (fallback chain / load-balance) re-derives its thinking
# params here. It pops every thinking key first, so "disabled" was expressed
# the same wrong way: by leaving the key popped.

def _swap(body, new_model, thinking_format=''):
    """Drive the REAL swap helper.

    ``thinking_format`` is a required positional on the production signature
    (''/auto = derive the dialect from the model name), and ``effort`` is read
    off the BODY rather than passed — so callers seed it into the body dict.
    """
    from lib.llm_dispatch.api import _readjust_thinking_params
    _readjust_thinking_params(body, new_model, thinking_format)
    return body


@pytest.mark.parametrize('model', OPUS5_IDS)
def test_swap_onto_opus5_with_thinking_off_sends_disabled(model):
    """★ Model-swap path. GLM body (thinking off) → Opus 5 must say disabled."""
    body = {'model': 'glm-5.1', 'messages': [dict(m) for m in _MSGS],
            'thinking': {'type': 'disabled'}, 'temperature': 0.7}
    _swap(body, model)
    assert body.get('thinking') == {'type': 'disabled'}, (
        'swap onto %s dropped the disable intent (thinking=%r). The pop-then-'
        're-apply cycle must RE-STATE disabled for Opus 5, not leave it '
        'implied.' % (model, body.get('thinking')))
    assert 'effort' not in body, 'disabled+effort is HTTP 400'


@pytest.mark.parametrize('model', OPUS5_IDS)
def test_swap_onto_opus5_thinking_on_unchanged(model):
    """Swap path's thinking-ON branch stays byte-identical."""
    body = {'model': 'glm-5.1', 'messages': [dict(m) for m in _MSGS],
            'thinking': {'type': 'enabled'}, 'temperature': 1.0,
            'effort': 'max'}
    _swap(body, model)
    assert body.get('thinking') == {'type': 'adaptive', 'display': 'summarized'}
    assert body.get('effort') == 'max'
    assert 'temperature' not in body


def test_swap_onto_legacy_claude_wire_unchanged():
    """Swap onto pre-adaptive Claude keeps the omit wire."""
    body = {'model': 'glm-5.1', 'messages': [dict(m) for m in _MSGS],
            'thinking': {'type': 'disabled'}}
    _swap(body, 'claude-3-5-sonnet-20241022')
    assert 'thinking' not in body


# ══════════════════════════════════════════════════════════
#  Anthropic-native wire
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize('model', OPUS5_IDS)
def test_disable_form_survives_anthropic_translation(model):
    """The disable form must reach the Anthropic Messages wire intact.

    openai_body_to_anthropic forwards an allowlist of model-level params;
    ``thinking`` is on it. This pins that the DISABLED shape (not just the
    adaptive one) rides through — a narrowed allowlist would otherwise
    silently restore the bug on the oauth_claude provider.
    """
    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    out = openai_body_to_anthropic(_build(model, thinking_enabled=False))
    assert out.get('thinking') == {'type': 'disabled'}
    assert 'effort' not in out


def main():
    raise SystemExit(pytest.main([__file__, '-v']))


if __name__ == '__main__':
    main()
