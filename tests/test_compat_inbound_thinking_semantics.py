#!/usr/bin/env python3
"""tests/test_compat_inbound_thinking_semantics.py — the INBOUND half of Opus 5 adaptive thinking.

Companion to tests/test_claude_opus5_thinking_disabled.py, which fixed the
OUTBOUND direction (what Tofu sends upstream). This suite pins the INBOUND
direction: what Tofu makes of an Opus-5-era request arriving at its own
Anthropic-compat endpoint (``routes/compat_anthropic.py`` → ``/v1/messages``)
and OpenAI-compat endpoint.

Regression class pinned
=======================
``translate_anthropic_request`` recognised exactly ONE thinking shape:
``{'type': 'enabled'}``. That is the PRE-4.7 form — the very one Opus 4.7+
deleted and now rejects with HTTP 400. Every shape an Opus-5-era client
actually sends fell through the branch entirely:

    client sends                                  parsed as
    --------------------------------------------  ------------------------
    thinking={'type':'adaptive'} + output_config   thinkingEnabled=None
    thinking={'type':'adaptive'}                   thinkingEnabled=None
    thinking={'type':'disabled'}                   thinkingEnabled=None
    (no thinking field — Opus 5 default is ON)     thinkingEnabled=None

``output_config.effort`` — the documented home of the effort dial on the
adaptive generation — was read NOWHERE in the repo.

★ The severe one is ``disabled``. Measured end-to-end through the real
``_resolve_model_config`` on HEAD (model=yuju-claude-opus-5-evaDaily):

    thinking={'type':'disabled'}  →  cfg{En=None,D=None}  →  thinking_enabled=TRUE

Downstream defaults ``thinkingEnabled`` to True on the direct-model path, so a
client explicitly asking to turn thinking OFF got it turned ON. Not merely
dropped — INVERTED. Same root cause as the outbound bug: treating an
unrecognised/absent value as "no opinion" when it is in fact a stated one.

Design of the fix (what these tests hold)
-----------------------------------------
* ``adaptive`` → enabled, and the effort dial passes through UNMAPPED across
  the five documented rungs (low/medium/high/xhigh/max). No budget_tokens
  banding — that table exists to approximate a rung from a token count, and
  an explicit rung needs no approximation.
* ``disabled`` → explicitly False, never conflated with "absent".
* ABSENT is resolved from the model's REAL vendor default, because this
  endpoint emulates the Anthropic Messages API: adaptive-generation Claude
  (is_claude_opus_47) defaults ON, pre-4.7 Claude defaults OFF, and a
  non-Claude model is left UNSET — we are not emulating some other vendor's
  default, so the existing downstream default must keep applying. That third
  branch is deliberate, not an oversight; test_absent_non_claude_left_unset
  is what stops a future "simplify" from collapsing it into False and
  silently disabling thinking for every GLM/Qwen caller.
* ``{'type':'enabled'}`` + budget_tokens keeps its original banding so
  pre-4.7 clients are untouched.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_compat_inbound_thinking_semantics.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

# Adaptive generation (is_claude_opus_47 → True). Both the production gateway
# id and the clean API id, since the bare-major regex must parse each as >= 4.7.
ADAPTIVE_MODELS = ('yuju-claude-opus-5-evaDaily', 'claude-opus-5',
                   'aws.claude-opus-4.7', 'aws.claude-opus-4.8')
# Pre-adaptive Claude — real API default is thinking OFF.
LEGACY_CLAUDE_MODELS = ('aws.claude-opus-4.6', 'claude-3-5-sonnet-20241022')
# Not Anthropic at all — we emulate no vendor default here.
NON_CLAUDE_MODELS = ('glm-5.1', 'qwen3.5-plus', 'kimi-k3')

# The five documented Opus 5 effort rungs. All are legal Tofu thinkingDepth
# values (lib/agent_options.py _FIELDS), so the mapping is identity.
EFFORT_RUNGS = ('low', 'medium', 'high', 'xhigh', 'max')


def _T(body):
    from lib.compat.anthropic import translate_anthropic_request
    _msgs, cfg, _opts = translate_anthropic_request(body)
    return cfg


def _body(model, **extra):
    b = {'model': model, 'messages': [{'role': 'user', 'content': 'hi'}]}
    b.update(extra)
    return b


# ══════════════════════════════════════════════════════════
#  1. adaptive → enabled, effort passes through
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize('model', ADAPTIVE_MODELS)
def test_adaptive_is_recognised_as_enabled(model):
    """★ `adaptive` is THE thinking form on Opus 4.7+ and must be understood."""
    cfg = _T(_body(model, thinking={'type': 'adaptive'}))
    assert cfg.get('thinkingEnabled') is True, (
        'model=%s: thinking={"type":"adaptive"} parsed as thinkingEnabled=%r. '
        'adaptive is the ONLY enable form on the 4.7+ generation — the legacy '
        '"enabled" it replaced now returns HTTP 400 upstream.'
        % (model, cfg.get('thinkingEnabled')))


@pytest.mark.parametrize('effort', EFFORT_RUNGS)
def test_output_config_effort_maps_to_depth(effort):
    """★ output_config.effort is the DOCUMENTED home of the dial — read it."""
    cfg = _T(_body('claude-opus-5', thinking={'type': 'adaptive'},
                   output_config={'effort': effort}))
    assert cfg.get('thinkingDepth') == effort, (
        'output_config.effort=%r produced thinkingDepth=%r. All five rungs are '
        'legal Tofu depths, so the mapping must be identity — no banding, no '
        'clamping.' % (effort, cfg.get('thinkingDepth')))
    assert cfg.get('thinkingEnabled') is True


@pytest.mark.parametrize('effort', EFFORT_RUNGS)
def test_top_level_effort_maps_to_depth(effort):
    """Top-level `effort` is the position our own outbound wire uses (and the
    gateway honours it), so an inbound body carrying it must parse too."""
    cfg = _T(_body('claude-opus-5', thinking={'type': 'adaptive'},
                   effort=effort))
    assert cfg.get('thinkingDepth') == effort


def test_output_config_wins_over_top_level_effort():
    """When both are present the DOCUMENTED position is authoritative."""
    cfg = _T(_body('claude-opus-5', thinking={'type': 'adaptive'},
                   output_config={'effort': 'max'}, effort='low'))
    assert cfg.get('thinkingDepth') == 'max'


def test_unknown_effort_is_ignored_not_forwarded():
    """A rung we don't know must not be smuggled into cfg — thinkingDepth is a
    closed enum downstream, and an unknown value would fail validation far
    from here."""
    cfg = _T(_body('claude-opus-5', thinking={'type': 'adaptive'},
                   output_config={'effort': 'turbo'}))
    assert cfg.get('thinkingDepth') in (None, ''), (
        'unknown effort leaked as thinkingDepth=%r' % cfg.get('thinkingDepth'))
    assert cfg.get('thinkingEnabled') is True, 'adaptive still enables thinking'


def test_effort_without_thinking_field_still_read():
    """Opus 5 thinks by default, so a client may send effort with NO thinking
    block at all. The dial must still be honoured."""
    cfg = _T(_body('claude-opus-5', output_config={'effort': 'xhigh'}))
    assert cfg.get('thinkingDepth') == 'xhigh'
    assert cfg.get('thinkingEnabled') is True


# ══════════════════════════════════════════════════════════
#  2. disabled → explicitly False (the INVERSION bug)
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize('model', ADAPTIVE_MODELS + LEGACY_CLAUDE_MODELS
                         + NON_CLAUDE_MODELS)
def test_disabled_is_explicitly_false(model):
    """★ THE SEVERE ONE. `disabled` is a STATED intent, not an absence.

    Measured on HEAD before the fix: cfg{En=None} → _resolve_model_config
    returned thinking_enabled=True. The client asked for OFF and got ON.
    """
    cfg = _T(_body(model, thinking={'type': 'disabled'}))
    assert cfg.get('thinkingEnabled') is False, (
        'model=%s: thinking={"type":"disabled"} parsed as thinkingEnabled=%r. '
        'It must be an explicit False — leaving it None lets the downstream '
        'default flip it back ON, inverting the caller\'s explicit request.'
        % (model, cfg.get('thinkingEnabled')))


def test_disabled_resolves_to_off_end_to_end():
    """Drive the REAL downstream resolver, not just the translator — that is
    where the inversion actually bit."""
    from lib.tasks_pkg.model_config import _resolve_model_config
    cfg = _T(_body('yuju-claude-opus-5-evaDaily',
                   thinking={'type': 'disabled'}))
    resolved = _resolve_model_config(cfg, 'test-task')
    assert resolved['thinking_enabled'] is False, (
        'end-to-end: an explicit disable still resolved to thinking_enabled=%r'
        % resolved['thinking_enabled'])


# ══════════════════════════════════════════════════════════
#  3. absent → the model's REAL vendor default
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize('model', ADAPTIVE_MODELS)
def test_absent_on_adaptive_generation_means_on(model):
    """Opus 4.7+ runs adaptive thinking by default when no field is sent."""
    cfg = _T(_body(model))
    assert cfg.get('thinkingEnabled') is True, (
        'model=%s: no thinking field parsed as thinkingEnabled=%r. The '
        'adaptive generation thinks by DEFAULT — the compat surface must '
        'mirror the real API.' % (model, cfg.get('thinkingEnabled')))


@pytest.mark.parametrize('model', LEGACY_CLAUDE_MODELS)
def test_absent_on_pre_adaptive_claude_means_off(model):
    """Pre-4.7 Claude defaults thinking OFF — mirror that, don't guess."""
    cfg = _T(_body(model))
    assert cfg.get('thinkingEnabled') is False, (
        'model=%s: no thinking field parsed as thinkingEnabled=%r; pre-4.7 '
        'Claude defaults OFF.' % (model, cfg.get('thinkingEnabled')))


@pytest.mark.parametrize('model', NON_CLAUDE_MODELS)
def test_absent_non_claude_left_unset(model):
    """★ Deliberate third branch — do NOT collapse this into False.

    We emulate Anthropic's default only for Anthropic models. A GLM/Qwen/Kimi
    caller that says nothing must keep the EXISTING downstream default; forcing
    False here would silently disable thinking for every non-Claude client of
    this endpoint.
    """
    cfg = _T(_body(model))
    assert 'thinkingEnabled' not in cfg or cfg.get('thinkingEnabled') is None, (
        'model=%s: a non-Claude model with no thinking field had '
        'thinkingEnabled forced to %r. Leave it unset.'
        % (model, cfg.get('thinkingEnabled')))


# ══════════════════════════════════════════════════════════
#  4. legacy enabled + budget_tokens untouched
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize('budget,expected', [
    (4096, 'medium'), (8192, 'medium'),
    (12000, 'high'), (16384, 'high'),
    (24000, 'xhigh'), (32768, 'xhigh'),
    (60000, 'max'),
])
def test_legacy_budget_banding_preserved(budget, expected):
    """Pre-4.7 clients keep the byte-identical behaviour they have today."""
    cfg = _T(_body('claude-3-5-sonnet-20241022',
                   thinking={'type': 'enabled', 'budget_tokens': budget}))
    assert cfg.get('thinkingEnabled') is True
    assert cfg.get('thinkingDepth') == expected, (
        'budget=%d banded to %r, expected %r' % (budget, cfg.get('thinkingDepth'),
                                                 expected))


def test_legacy_enabled_without_budget_still_enables():
    cfg = _T(_body('claude-3-5-sonnet-20241022', thinking={'type': 'enabled'}))
    assert cfg.get('thinkingEnabled') is True


def test_explicit_effort_beats_budget_banding():
    """An explicit rung needs no approximation from a token count."""
    cfg = _T(_body('claude-opus-5',
                   thinking={'type': 'enabled', 'budget_tokens': 4096},
                   output_config={'effort': 'max'}))
    assert cfg.get('thinkingDepth') == 'max'


# ══════════════════════════════════════════════════════════
#  5. OpenAI-compat depth ladder is lossy
# ══════════════════════════════════════════════════════════
#
# lib/compat/openai.py mapped low→medium, medium→high, high→max — the whole
# ladder shifted up one rung, and `xhigh` was absent so it fell through the
# `.get(eff, eff)` default by accident rather than by design. Opus 5 exposes
# all five rungs and Tofu's depth enum has all five, so the compression is
# pure loss: a caller asking for `low` got `medium` (more tokens than asked).

def _TO(body):
    from lib.compat.openai import translate_openai_request
    _msgs, cfg, _opts = translate_openai_request(body)
    return cfg


@pytest.mark.parametrize('effort', EFFORT_RUNGS)
def test_openai_reasoning_effort_is_identity(effort):
    cfg = _TO({'model': 'claude-opus-5', 'messages': [], 'reasoning_effort': effort})
    assert cfg.get('thinkingDepth') == effort, (
        'reasoning_effort=%r became thinkingDepth=%r — the ladders match rung '
        'for rung, so any shift is silent over/under-spend.'
        % (effort, cfg.get('thinkingDepth')))
    assert cfg.get('thinkingEnabled') is True


def test_openai_minimal_maps_to_lowest_rung():
    """`minimal` has no Tofu rung of its own; the honest floor is `low`, not
    `medium` (which would spend MORE than the caller asked for)."""
    cfg = _TO({'model': 'claude-opus-5', 'messages': [], 'reasoning_effort': 'minimal'})
    assert cfg.get('thinkingDepth') == 'low'


def test_openai_nested_reasoning_effort_still_read():
    cfg = _TO({'model': 'claude-opus-5', 'messages': [],
               'reasoning': {'effort': 'xhigh'}})
    assert cfg.get('thinkingDepth') == 'xhigh'


def main():
    raise SystemExit(pytest.main([__file__, '-v']))


if __name__ == '__main__':
    main()
