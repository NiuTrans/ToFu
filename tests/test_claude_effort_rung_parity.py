#!/usr/bin/env python3
"""tests/test_claude_effort_rung_parity.py — every rung is SAID, and both outbound paths say it identically.

Regression class pinned
=======================
Closes the last hole in the Opus 5 adaptation, and it sat on the DEFAULT rung.

``lib/llm/body/_build.py`` carried ``if _effort and _effort != 'medium'`` —
``medium`` was explicitly excluded and never reached the wire. That exclusion
was CORRECT when it was written: medium WAS the model default, so omitting it
was a free optimization. Claude Opus 5 moved the default to ``high``, and the
line's meaning silently changed from "save a few bytes" to "quietly upgrade
the user's Med to High".

``index.html`` marks ``data-depth="medium"`` as ``active`` on BOTH the desktop
(:795) and mobile (:1708) selectors — this is the rung most users ride on
every single turn.

LIVE gateway evidence (yuju-claude-opus-5-evaDaily via your-llm-gateway.example.com,
identical prompt, max_tokens=8000, adaptive thinking, n=6 per arm,
2026-07-26):

    Med as sent (effort DROPPED)  [2013,2336,2778,2847,3157,3353]  median 2812.5
    Med explicit (effort=medium)  [1236,1296,1845,1865,2556,3723]  median 1855.0
                                                            ratio ≈ 1.52x

Same root cause as the two fixes before it (8d7b6911 outbound thinking-off,
43dd1ecd inbound thinking semantics): an "omission implies the default"
assumption that Opus 5 invalidated.

★ The parity half is the durable part
-------------------------------------
Tofu builds a Claude request body in TWO places that must agree:

  1. ``lib.llm.body.build_body``                       — the primary path
  2. ``lib.llm_dispatch.api._readjust_thinking_params`` — the model-SWAP path
     (fallback chain / load balance), which pops every thinking key and
     re-derives them for the new model.

Before this fix they DISAGREED on exactly this rung: build_body dropped
``medium``, the swap path kept it. Identical (model, enabled, depth) input,
two different wires — which is a bug outright, not a style difference.
``test_both_outbound_paths_agree_on_every_rung`` is the guard that stops them
re-diverging; it is deliberately a cross-product over rungs x models rather
than a spot check, because the last divergence hid on one single rung.

Scope: pre-4.7 Claude keeps the byte-identical omit-medium wire. Those models
really do still default to medium, so sending it changes nothing upstream, and
the whole point of this suite is that a rung is only worth stating when
omitting it would mean something ELSE.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_claude_effort_rung_parity.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

# Adaptive generation — default effort is `high`, so EVERY rung must be stated.
ADAPTIVE_MODELS = ('yuju-claude-opus-5-evaDaily', 'claude-opus-5',
                   'aws.claude-opus-4.7', 'aws.claude-opus-4.8')
# Pre-adaptive Claude — default really is medium; omit-medium stays correct.
LEGACY_CLAUDE_MODELS = ('aws.claude-opus-4.6', 'claude-3-5-sonnet-20241022')

RUNGS = ('low', 'medium', 'high', 'xhigh', 'max')

_MSGS = [{'role': 'user', 'content': 'hi'}]


def _build(model, depth):
    from lib.llm.body import build_body
    return build_body(model, [dict(m) for m in _MSGS],
                      thinking_enabled=True, thinking_depth=depth)


def _swap(model, depth):
    """Drive the real swap helper with a foreign body carrying `depth`."""
    from lib.llm_dispatch.api import _readjust_thinking_params
    body = {'model': 'glm-5.1', 'messages': [dict(m) for m in _MSGS],
            'thinking': {'type': 'enabled'}, 'effort': depth,
            'temperature': 0.7}
    _readjust_thinking_params(body, model, '')
    return body


# ══════════════════════════════════════════════════════════
#  1. medium reaches the wire on the adaptive generation
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize('model', ADAPTIVE_MODELS)
def test_medium_is_stated_not_omitted(model):
    """★ THE BUG. Opus 5 defaults to `high`, so an omitted `medium` is an upgrade.

    Live-measured ~1.52x more completion tokens when the rung is dropped —
    on the rung the UI ships as the active default.
    """
    body = _build(model, 'medium')
    assert body.get('effort') == 'medium', (
        'model=%s depth=medium emitted effort=%r. The adaptive generation '
        'defaults to `high`, so omitting `medium` silently upgrades the user '
        "'s explicit choice (live-measured ~1.52x tokens)." % (model, body.get('effort')))


@pytest.mark.parametrize('model', ADAPTIVE_MODELS)
@pytest.mark.parametrize('rung', RUNGS)
def test_every_rung_reaches_the_wire(model, rung):
    """No rung may be silently dropped — the whole ladder is explicit now."""
    body = _build(model, rung)
    assert body.get('effort') == rung, (
        'model=%s depth=%s emitted effort=%r' % (model, rung, body.get('effort')))


@pytest.mark.parametrize('model', LEGACY_CLAUDE_MODELS)
def test_pre_adaptive_claude_keeps_omit_medium_wire(model):
    """Pre-4.7 Claude really does default to medium — omitting it means the
    same thing, so the historical wire is preserved byte-for-byte."""
    body = _build(model, 'medium')
    assert 'effort' not in body, (
        'model=%s gained effort=%r on the medium path; pre-adaptive Claude '
        'defaults to medium already.' % (model, body.get('effort')))


@pytest.mark.parametrize('model', LEGACY_CLAUDE_MODELS)
def test_pre_adaptive_claude_still_states_other_rungs(model):
    """Only `medium` is a no-op on the old generation; other rungs still ship
    (with xhigh downgraded to high, which those models don't support)."""
    assert _build(model, 'low').get('effort') == 'low'
    assert _build(model, 'max').get('effort') == 'max'
    assert _build(model, 'xhigh').get('effort') == 'high'


# ══════════════════════════════════════════════════════════
#  2. ★ the two outbound paths must never diverge again
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize('model', ADAPTIVE_MODELS + LEGACY_CLAUDE_MODELS)
@pytest.mark.parametrize('rung', RUNGS)
def test_both_outbound_paths_agree_on_every_rung(model, rung):
    """★ THE DURABLE GUARD.

    build_body and _readjust_thinking_params both produce a Claude body. For
    identical (model, thinking-enabled, depth) they must emit the identical
    `effort` key. They diverged on exactly one rung (`medium`) and nobody
    noticed, which is why this is a full cross-product and not a spot check.
    """
    built = _build(model, rung).get('effort')
    swapped = _swap(model, rung).get('effort')
    assert built == swapped, (
        'OUTBOUND PATH DIVERGENCE — model=%s depth=%s: build_body emitted '
        'effort=%r but the model-swap path emitted %r. Both build a Claude '
        'request; a body must not depend on WHICH path assembled it. Keep the '
        'two branches in sync (lib/llm/body/_build.py and '
        'lib/llm_dispatch/api.py::_readjust_thinking_params).'
        % (model, rung, built, swapped))


@pytest.mark.parametrize('model', ADAPTIVE_MODELS + LEGACY_CLAUDE_MODELS)
@pytest.mark.parametrize('rung', RUNGS)
def test_both_paths_agree_on_the_thinking_block_too(model, rung):
    """Same parity for the `thinking` block, so the guard covers the whole
    thinking surface rather than just the effort key."""
    built = _build(model, rung).get('thinking')
    swapped = _swap(model, rung).get('thinking')
    assert built == swapped, (
        'model=%s depth=%s: build_body thinking=%r vs swap path %r'
        % (model, rung, built, swapped))


def test_disabled_path_still_ships_no_effort():
    """Regression tie-back to 8d7b6911: making rungs explicit must NOT leak an
    effort key onto the disabled path — disabled + xhigh/max is HTTP 400."""
    from lib.llm.body import build_body
    for rung in RUNGS:
        body = build_body('claude-opus-5', [dict(m) for m in _MSGS],
                          thinking_enabled=False, thinking_depth=rung)
        assert body.get('thinking') == {'type': 'disabled'}
        assert 'effort' not in body, (
            'depth=%s leaked effort=%r onto the disabled path' % (rung, body.get('effort')))


def main():
    raise SystemExit(pytest.main([__file__, '-v']))


if __name__ == '__main__':
    main()
