"""tests/agent_verdict/test_vu_machine_token_strip.py — VU machine-control token strip (pt_0ae59e94).

The VU protocol carries done, progress, and verdict machine-control tokens.
All must be stripped through ONE
predicate (``lib.agent_verdict.strip_machine_tokens``, backed by ONE
registry) before a VU reply is persisted into conversation history — a
leaked token is re-read by the next turn as ordinary user text and the
model starts authoring the signal itself (production: 90 lines across 52
conversations).

The budget guard (``_record_vu_turn_and_check_budget``) is the ONE consumer
that must still see the raw PROGRESS line (it drives the
diminishing-returns ledger), so the strip happens AFTER ledger consumption
and BEFORE persistence — the guard reads the original, the DB stores the
clean copy. (The standalone interpreter that applied this ordering is
retired; the predicate and registry below remain the live single source
consumed by the Flow/VU paths.)

Layers pinned here:
  1. predicate unit behaviour (strip / keep= / idempotence / validation);
  2. the registry as single source of truth (all tokens, and the PROGRESS
     pattern IS the verdict parser's own regex — no re-implementation).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.agent_verdict import VU_DONE_SENTINEL, strip_machine_tokens
from lib.agent_verdict._handoff import _MACHINE_TOKEN_STRIP_PATTERNS

pytestmark = pytest.mark.unit

PROGRESS_SAMPLE = '[PROGRESS: resolved=2 remaining=3]'


# ── 1. Predicate unit behaviour ───────────────────────────────────────────

def test_strip_removes_both_tokens_and_keeps_prose():
    text = '继续修第三条。\n' + VU_DONE_SENTINEL + '\n' + PROGRESS_SAMPLE
    out = strip_machine_tokens(text)
    assert VU_DONE_SENTINEL not in out
    assert '[PROGRESS' not in out
    assert '继续修第三条。' in out


@pytest.mark.parametrize('tag', [
    '[VERDICT: STOP]', '[VERDICT: CONTINUE_WORKER]',
    '[VERDICT: CONTINUE_PLANNER]', '[verdict: continue]',
])
def test_review_strips_inline_verdict_without_discarding_following_findings(tag):
    from lib.agent_verdict import classify_verdict, parse_progress
    text = f'Finding before. {tag} Finding after.\n{PROGRESS_SAMPLE}'
    assert strip_machine_tokens(text) == 'Finding before.  Finding after.'
    assert parse_progress(text) == (2, 3)
    assert classify_verdict(text)['phase'] == classify_verdict(tag + '\n' + PROGRESS_SAMPLE)['phase']


def test_strip_idempotent_and_empty_safe():
    once = strip_machine_tokens('x\n' + PROGRESS_SAMPLE)
    assert strip_machine_tokens(once) == once
    assert strip_machine_tokens('') == ''
    assert strip_machine_tokens(None) is None


def test_strip_leaves_ordinary_text_untouched():
    text = '第一轮完成 2 项，剩 3 项。\n继续。'
    assert strip_machine_tokens(text) == text


def test_keep_progress_preserves_line_but_strips_done():
    text = 'go on\n' + VU_DONE_SENTINEL + '\n' + PROGRESS_SAMPLE
    out = strip_machine_tokens(text, keep=('progress_line',))
    assert VU_DONE_SENTINEL not in out
    assert PROGRESS_SAMPLE in out


def test_keep_unknown_label_raises():
    with pytest.raises(ValueError):
        strip_machine_tokens('x', keep=('not_a_token',))


# ── 2. Registry = single source of truth ──────────────────────────────────

def test_registry_covers_protocol_tokens():
    labels = {label for label, _ in _MACHINE_TOKEN_STRIP_PATTERNS}
    assert labels == {'vu_done_sentinel', 'progress_line', 'verdict'}
    pats = dict(_MACHINE_TOKEN_STRIP_PATTERNS)
    assert pats['vu_done_sentinel'].search(VU_DONE_SENTINEL)
    assert pats['progress_line'].search(PROGRESS_SAMPLE)


def test_registry_progress_pattern_is_the_verdict_parsers_own():
    # A re-implemented PROGRESS regex copy is exactly the hand-copy
    # divergence lib/agent_verdict exists to kill — the strip must match
    # what parse_progress matches, by construction (same object).
    from lib.agent_verdict import _PROGRESS_RE
    pats = dict(_MACHINE_TOKEN_STRIP_PATTERNS)
    assert pats['progress_line'] is _PROGRESS_RE
